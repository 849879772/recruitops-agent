"""Bounded entry discovery shared by configured and isolated candidate crawls.

The deadline limits discovery and subsequent attempts. Callbacks must honor their
remaining timeout; the configured core still runs inside its caller's killable
child process, which owns the hard overall timeout.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from time import perf_counter
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import runner
from .crawlers.base import crawl_budget, effective_crawl_timeout_seconds
from .crawlers.render import render_page
from .entry import diagnose_candidate_entry
from .models import CompanyConfig


MAX_ENTRY_CANDIDATES = 5
DEFAULT_ENTRY_TIMEOUT_SECONDS = 75.0


class _DiscoveryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _visible_control(node) -> bool:
    if node.get("data-recruitops-visible") == "false":
        return False
    for ancestor in [node, *node.parents]:
        if ancestor.get("hidden") is not None or str(ancestor.get("aria-hidden", "")).lower() == "true":
            return False
        if ancestor.name == "dialog" and not ancestor.has_attr("open"):
            return False
        style = re.sub(r"\s+", "", str(ancestor.get("style") or "")).lower()
        if re.search(r"(?:^|;)(?:display:none|visibility:hidden|opacity:0)(?:!important)?(?:;|$)", style):
            return False
    return True


def _entry_error(url: str) -> tuple[str, str] | None:
    diagnosis = diagnose_candidate_entry(url)
    if diagnosis.entry_kind == "form_application":
        return "form_application_only", diagnosis.reason
    if diagnosis.entry_kind == "invalid_entry":
        code = "login_required" if diagnosis.reason.startswith("login_page:") else "invalid_entry"
        return code, diagnosis.reason
    return None


def _sms_login_wall(soup: BeautifulSoup) -> bool:
    for form in soup.select("form") or [soup]:
        fields = [node for node in form.select("input") if _visible_control(node)]
        labels = [" ".join(str(node.get(key, "")) for key in ("type", "name", "placeholder", "autocomplete"))
                  for node in fields]
        if (
            any(re.search(r"\btel\b|phone|mobile|手机号|手机号码", label, re.I) for label in labels)
            and any(re.search(r"one-time-code|otp|sms|验证码|短信", label, re.I) for label in labels)
            and re.search(r"登录|log\s*in|sign\s*in", form.get_text(" ", strip=True), re.I)
        ):
            return True
    return False


def discover_recruitment_entries(
    source_url: str,
    timeout_seconds: float,
    *,
    render: Callable[..., Any] | None = None,
    http_get: Callable[..., Any] | None = None,
    http_only: bool = False,
) -> list[str]:
    """Inspect a public entry over HTTP, rendering only when links need JavaScript.

    Only observed links to known adapters are returned. No URL synthesis, form
    submission, authenticated browser session, or challenge bypass is attempted.
    ``http_only`` is used for the short preflight before a generic crawl.
    """
    error = _entry_error(source_url)
    if error:
        raise _DiscoveryError(*error)
    deadline = perf_counter() + max(0.0, timeout_seconds)
    render = render or render_page
    http_get = http_get or requests.get
    base_url = source_url
    http_deadline = perf_counter() + min(10.0, max(0.0, deadline - perf_counter()) * 0.4)
    try:
        for _ in range(4):
            remaining = min(deadline, http_deadline) - perf_counter()
            if remaining <= 0:
                break
            response = http_get(
                base_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9"},
                timeout=min(6.0, remaining), allow_redirects=False,
            )
            if response.status_code == 429:
                raise _DiscoveryError("rate_limited", "The public entry is temporarily rate limited.")
            if response.status_code in {401, 403}:
                raise _DiscoveryError("access_denied", "The public entry requires authorization.")
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise _DiscoveryError("entry_discovery_failed", "Redirect has no destination.")
                base_url = urljoin(base_url, location)
                error = _entry_error(base_url)
                if error:
                    raise _DiscoveryError(*error)
                continue
            response.raise_for_status()
            candidates = _entry_candidates(response.text, base_url, source_url)
            if candidates:
                return candidates
            break
        else:
            raise _DiscoveryError("entry_discovery_failed", "Entry redirect limit reached.")
    except (requests.RequestException, OSError, TimeoutError):
        pass
    if http_only:
        return []
    return _render_entry_candidates(base_url, source_url, deadline, render)


def _render_entry_candidates(
    base_url: str, source_url: str, deadline: float, render: Callable[..., Any],
) -> list[str]:
    remaining = deadline - perf_counter()
    if remaining <= 0:
        raise _DiscoveryError("timeout", "Entry discovery deadline exhausted.")
    try:
        page_html = render(
            base_url, timeout_ms=max(1, int(min(30.0, remaining) * 1_000)),
            extra_wait_ms=0, scroll_times=0,
            annotate_visibility=True,
        )
    except (OSError, RuntimeError, TimeoutError):
        page_html = None
    if perf_counter() >= deadline:
        raise _DiscoveryError("timeout", "Entry discovery deadline exhausted.")
    return _entry_candidates(page_html, base_url, source_url)


def _entry_candidates(page_html: Any, base_url: str, source_url: str) -> list[str]:
    soup = BeautifulSoup(str(page_html or ""), "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if any(_visible_control(node) for node in soup.select('input[type="password"]')) or _sms_login_wall(soup):
        raise _DiscoveryError("login_required", "The entry is a login page.")
    if re.search(r"captcha|verify you are human|security verification|验证码|安全验证|人机验证", title, re.I) or any(_visible_control(node) for node in soup.select(
        'iframe[src*="captcha"], form[action*="captcha"], #captcha, #challenge-form'
    )):
        raise _DiscoveryError("captcha_required", "The entry requires a CAPTCHA or verification.")
    candidates: dict[str, int] = {}
    for anchor in soup.find_all("a", href=True):
        if not _visible_control(anchor):
            continue
        href = str(anchor.get("href") or "").strip()
        if not href or href == "#":
            continue
        url = urljoin(base_url, href)
        diagnosis = diagnose_candidate_entry(url)
        if url == source_url or diagnosis.entry_kind != "existing_adapter":
            continue
        signal = f"{anchor.get_text(' ', strip=True)} {url}"
        score = 100 if re.search(r"校园招聘|校招|应届|campus", signal, re.I) else 0
        if re.search(r"社会招聘|社招|experienced", signal, re.I):
            score -= 40
        candidates[url] = max(score, candidates.get(url, score))
    return sorted(candidates, key=lambda url: (-candidates[url], url))[:MAX_ENTRY_CANDIDATES]


def _empty_evidence(code: str | None = None, message: str | None = None) -> dict[str, Any]:
    return {
        "jobs": [], "raw_job_count": 0, "pagination_complete": False,
        "completeness_known": False, "pages_seen": 0, "total_pages": None,
        "has_more": False, "advertised_total": None, "termination_reasons": [],
        "source_runs": [], "failures": [], "effective_source_urls": [],
        "error_code": code, "error_message": message,
    }


def _partial_evidence(result: dict[str, Any]) -> bool:
    return bool(
        result.get("has_more") or result.get("advertised_total")
        or (result.get("completeness_known") and result.get("pagination_complete") is False
            and result.get("pages_seen"))
        or any(run.get("observed_total") for run in result.get("source_runs") or [])
    )


def crawl_with_entry_discovery(
    source_url: str,
    *,
    crawl: Callable[[str, str, float], dict[str, Any]],
    crawler_key: str | None = None,
    timeout_seconds: float = DEFAULT_ENTRY_TIMEOUT_SECONDS,
    discover: Callable[[str, float], list[str]] | None = None,
) -> dict[str, Any]:
    """Share routing while leaving subprocess isolation with the crawl callback.

    ``source_url`` always identifies the input. ``crawl_source_url`` identifies
    the selected attempt; ``entry_attempts`` retains other attempts' evidence
    without duplicating job payloads or mixing their pagination into that run.
    """
    deadline = perf_counter() + max(0.0, timeout_seconds)
    diagnosis = diagnose_candidate_entry(source_url)
    key = crawler_key or diagnosis.crawler_key or "render"
    if key in {"render", "static_html"} and diagnosis.entry_kind == "existing_adapter":
        key = diagnosis.crawler_key or key
    best = _empty_evidence()
    selected_url, selected_key = source_url, key
    attempts: list[dict[str, Any]] = []
    effective_urls: list[str] = []
    pretried_urls: set[str] = set()
    http_preflight_done = False
    preflight_base_url = source_url
    discovery_error: tuple[str, str] | None = None

    def finish() -> dict[str, Any]:
        result = {**best}
        code = result.get("error_code")
        if not code and not result["jobs"]:
            execution_failure = next(
                (
                    str(reason)
                    for reason in result.get("failures") or []
                    if isinstance(reason, str)
                    and reason
                    not in {
                        "adapter_did_not_report_completeness",
                        "adapter_reported_incomplete",
                    }
                ),
                None,
            )
            if discovery_error:
                code, result["error_message"] = discovery_error
            elif (
                result.get("completeness_known") is True
                and result.get("pagination_complete") is True
                and not result.get("has_more")
                and result.get("advertised_total") == 0
            ):
                result["run_reason"] = "activity_empty"
            elif execution_failure:
                code = execution_failure
            elif diagnose_candidate_entry(selected_url).entry_kind == "existing_adapter":
                code = "adapter_variant_unsupported"
            else:
                code = "recruitment_entry_discovery_required" if diagnosis.entry_kind == "entry_discovery_required" else "site_adapter_required"
        result.update(
            source_url=source_url,
            crawl_source_url=selected_url,
            discovered_entry_url=selected_url if selected_url != source_url else None,
            crawler_key=selected_key,
            effective_source_urls=effective_urls,
            entry_attempts=attempts,
            error_code=code,
        )
        return result

    error = _entry_error(source_url)
    if error:
        best = _empty_evidence(*error)
        return finish()

    def attempt(url: str, adapter: str) -> None:
        nonlocal best, selected_url, selected_key
        remaining = deadline - perf_counter()
        if remaining <= 0:
            raise _DiscoveryError("timeout", "Entry crawl deadline exhausted.")
        try:
            value = crawl(url, adapter, remaining)
            if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
                raise ValueError("entry crawler must return a dictionary containing jobs")
            result = {**_empty_evidence(), **value}
            result["raw_job_count"] = len(result["jobs"])
        except (subprocess.TimeoutExpired, TimeoutError, requests.Timeout) as exc:
            result = _empty_evidence("timeout", str(exc)[-1_000:])
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            result = _empty_evidence("crawler_failed", str(exc)[-1_000:])
        attempts.append({
            **{name: value for name, value in result.items() if name != "jobs"},
            "source_url": url, "crawler_key": adapter,
        })
        for effective_url in [url, *(result.get("effective_source_urls") or [])]:
            if effective_url not in effective_urls:
                effective_urls.append(effective_url)
        if len(attempts) == 1 or result["jobs"] or (
            not _partial_evidence(best) and (not result.get("error_code") or best.get("error_code"))
        ):
            best, selected_url, selected_key = result, url, adapter

    try:
        if key == "render" and discover is None and diagnosis.entry_kind != "existing_adapter":
            remaining = deadline - perf_counter()
            if remaining <= 0:
                raise _DiscoveryError("timeout", "Entry crawl deadline exhausted.")
            http_preflight_done = True

            def preflight_get(url: str, **kwargs: Any) -> Any:
                nonlocal preflight_base_url
                preflight_base_url = url
                return requests.get(url, **kwargs)

            # A short HTTP preflight can route a public campus link directly to
            # its adapter, without starting a generic Chromium crawl first.
            for url in discover_recruitment_entries(
                source_url, min(5.0, remaining), http_get=preflight_get,
                http_only=True,
            ):
                candidate = diagnose_candidate_entry(url)
                if candidate.entry_kind != "existing_adapter" or not candidate.crawler_key:
                    continue
                if source_url not in effective_urls:
                    effective_urls.append(source_url)
                attempt(url, candidate.crawler_key)
                pretried_urls.add(url)
                if best["jobs"] or best.get("error_code") in {"login_required", "captcha_required", "access_denied"}:
                    return finish()
        attempt(source_url, key)
        # Even incomplete jobs are evidence, not a reason to replace this run.
        if best["jobs"] or key not in {"render", "static_html"}:
            return finish()
        if best.get("error_code") in {"login_required", "captcha_required", "access_denied"}:
            return finish()
        remaining = deadline - perf_counter()
        if remaining <= 0:
            raise _DiscoveryError("timeout", "Entry crawl deadline exhausted.")
        if discover is not None:
            candidates = discover(source_url, remaining)
        elif http_preflight_done:
            candidates = _render_entry_candidates(
                preflight_base_url, source_url, deadline, render_page,
            )
        else:
            candidates = discover_recruitment_entries(source_url, remaining)
        seen = {source_url, *pretried_urls}
        tried = len(pretried_urls)
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            candidate = diagnose_candidate_entry(url)
            if candidate.entry_kind != "existing_adapter" or not candidate.crawler_key:
                continue
            if tried >= MAX_ENTRY_CANDIDATES:
                break
            attempt(url, candidate.crawler_key)
            tried += 1
            if best["jobs"] or best.get("error_code") in {"login_required", "captcha_required", "access_denied"}:
                break
    except _DiscoveryError as exc:
        discovery_error = exc.code, str(exc)
    except (TimeoutError, requests.Timeout, subprocess.TimeoutExpired) as exc:
        discovery_error = "timeout", str(exc)[-1_000:]
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        discovery_error = "entry_discovery_failed", str(exc)[-1_000:]
    return finish()


def crawl_company_with_entry_discovery(
    company: CompanyConfig | Mapping[str, Any],
    *,
    crawler_map: Mapping[str, type] | None = None,
) -> dict[str, Any]:
    """Wrap the core runner without dropping its jobs or completeness evidence."""
    config = CompanyConfig.from_legacy(company)

    def crawl(url: str, key: str, remaining: float) -> dict[str, Any]:
        payload = config.to_dict()
        payload.update(careers_url=url, crawler=key)
        if "crawl_timeout_seconds" in payload:
            payload["crawl_timeout_seconds"] = min(
                remaining,
                effective_crawl_timeout_seconds(
                    float(payload["crawl_timeout_seconds"])
                ),
            )
        if url != config.careers_url:
            # Campaign URLs belong to the original adapter. Keep OC authorization
            # (including source_cohort_url) unchanged while following a real link.
            payload.update(campaign_url=url, campaign_urls=[], link_kind="")
        with crawl_budget(remaining):
            return runner.crawl_company_with_evidence(payload, crawler_map=crawler_map)

    configured_timeout = float(
        config.get("crawl_timeout_seconds", DEFAULT_ENTRY_TIMEOUT_SECONDS)
    )
    return crawl_with_entry_discovery(
        config.careers_url, crawl=crawl, crawler_key=config.crawler,
        timeout_seconds=effective_crawl_timeout_seconds(configured_timeout),
    )


# Compatibility for callers which previously imported the OC private helper.
_discover_recruitment_entries = discover_recruitment_entries


__all__ = ["crawl_company_with_entry_discovery", "crawl_with_entry_discovery", "discover_recruitment_entries"]
