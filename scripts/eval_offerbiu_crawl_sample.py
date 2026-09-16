"""Bounded, read-only OfferBiu sample crawl evaluation."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import multiprocessing as mp
from queue import Empty
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.recruitment_core.jd_capture import assess_jd_capture
from scripts.eval_offerbiu_link_quality import classify_apply_url
from packages.tools.oc_candidates import (
    SubprocessCandidateCrawlerProcess,
    diagnose_candidate_entry,
)


MAX_BODY_BYTES = 1_048_576
MAX_PREFLIGHT_SECONDS = 15.0
MAX_REDIRECTS = 5
MAX_TIMEOUT_SECONDS = 90.0
PROCESS_CLEANUP_RESERVE_SECONDS = 1.0
SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "auth", "authorization", "cookie", "key",
    "password", "secret", "session", "sig", "signature", "token",
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
RESTRICTED_STATUSES = {401, 403, 407, 429, 451}


def _redact_url(value: object) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        if parsed.username or parsed.password:
            return "<credential-url>"
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if any(key.casefold() in SENSITIVE_QUERY_KEYS for key, _ in pairs):
            pairs = [
                (key, "<redacted>" if key.casefold() in SENSITIVE_QUERY_KEYS else item)
                for key, item in pairs
            ]
            parsed = parsed._replace(query=urlencode(pairs))
        return urlunsplit(parsed)
    except ValueError:
        return "<invalid-url>"


def _safe_sample(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.pop("_selection", None)
    result.pop("_company_key", None)
    value = result.get("applyUrl")
    if isinstance(value, (list, tuple)):
        result["applyUrl"] = [_redact_url(item) for item in value]
    elif value is not None:
        result["applyUrl"] = _redact_url(value)
    raw_record = result.get("raw_record")
    if isinstance(raw_record, Mapping):
        result["raw_record"] = _safe_sample(raw_record)
    return result


def _url_guard(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return "malformed_url"
    if parsed.scheme.casefold() not in {"http", "https"}:
        return "non_http_url"
    if not parsed.hostname or parsed.username or parsed.password:
        return "credential_or_missing_host"
    host = parsed.hostname.casefold().rstrip(".")
    if port is not None and not 1 <= port <= 65535:
        return "invalid_port"
    if (
        host in {"localhost", "host.docker.internal"}
        or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa"))
    ):
        return "private_or_loopback_host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return "private_or_non_global_address"
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold() in SENSITIVE_QUERY_KEYS for key, _ in pairs):
        return "credential_url"
    return None


def _static_destination_reason(url: str) -> str | None:
    parsed = urlsplit(url)
    if (parsed.hostname or "").startswith("admin.") and parsed.path.rstrip("/").endswith("/edit"):
        return "non_recruitment_admin_entry"
    classification = classify_apply_url(url)
    if classification == "wechat_article":
        return "known_article"
    if classification == "form":
        return "known_form"
    try:
        diagnosis = diagnose_candidate_entry(url)
    except Exception:
        diagnosis = None
    if diagnosis is not None and diagnosis.entry_kind == "form_application":
        return "known_form"
    return None


def _clear_cookies(session: object) -> None:
    cookies = getattr(session, "cookies", None)
    clear = getattr(cookies, "clear", None)
    if callable(clear):
        clear()


def _read_body(response: object) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    truncated = False
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        for chunk in iterator(chunk_size=64 * 1024):
            if not chunk:
                continue
            data = chunk.encode("utf-8", "replace") if isinstance(chunk, str) else bytes(chunk)
            remaining = MAX_BODY_BYTES + 1 - size
            if remaining <= 0:
                truncated = True
                break
            chunks.append(data[:remaining])
            size += min(len(data), remaining)
            if len(data) > remaining or size > MAX_BODY_BYTES:
                truncated = True
                break
    else:
        data = getattr(response, "content", b"")
        data = data.encode("utf-8", "replace") if isinstance(data, str) else bytes(data)
        truncated = len(data) > MAX_BODY_BYTES
        chunks.append(data[:MAX_BODY_BYTES])
    return b"".join(chunks)[:MAX_BODY_BYTES], truncated


def _page_observation(
    url: str,
    status_code: int,
    body: bytes,
    title: str,
    content_type: str,
) -> dict[str, Any]:
    text = body.decode("utf-8", "replace")
    soup = BeautifulSoup(text, "html.parser")
    page_title = " ".join((soup.title.get_text(" ", strip=True) if soup.title else title).split())
    for tag in soup(["script", "style", "template"]):
        tag.decompose()
    visible = " ".join(soup.stripped_strings)
    lowered = f"{page_title} {visible}".casefold()
    path = urlsplit(url).path.casefold()
    strong_restriction = bool(re.search(
        r"请先(?:登录|登陆)|登录后(?:才能|方可|继续)|"
        r"(?:完成|请完成)(?:安全|人机)?验证(?:后)?(?:才能|方可|继续)|"
        r"sign in to continue|access denied|security check|account locked|"
        r"(?:verification|captcha).{0,40}(?:required|to continue)",
        lowered,
    ))
    if status_code in RESTRICTED_STATUSES or re.search(
        r"/(?:login|signin|captcha)(?:/|$)", path
    ) or strong_restriction:
        return {
            "page_kind": "login_or_captcha",
            "title": page_title[:200],
            "signals": ["access_restricted"],
            "js_shell": False,
        }
    static_reason = _static_destination_reason(url)
    if static_reason:
        return {
            "page_kind": "article_or_form",
            "title": page_title[:200],
            "signals": [static_reason],
            "js_shell": False,
        }
    recruitment = bool(re.search(
        r"校园招聘|社会招聘|招聘|职位|岗位|career|recruit|talent|join us|job|position|apply",
        lowered,
    ))
    listing = bool(re.search(
        r"职位列表|岗位列表|job list|position list|查看详情|申请职位|location|工作地点|"
        r"/(?:jobs?|positions?|campus|careers?)(?:/|$)",
        f"{lowered} {path}",
    ))
    js_shell = len(visible) < 100 and bool(re.search(
        r"<script\b|id=[\"'](?:app|root|__next)|data-reactroot", text, re.I
    ))
    if js_shell:
        kind = "unknown"
        signals = ["js_shell_or_no_visible_jobs"]
    elif listing:
        kind = "job_listing_possible"
        signals = ["listing_markers"]
    elif recruitment:
        kind = "recruitment_navigation"
        signals = ["recruitment_markers"]
    elif urlsplit(url).path in {"", "/"}:
        kind = "root_homepage"
        signals = ["root_path"]
    else:
        kind = "unknown"
        signals = []
    if re.search(r"captcha|recaptcha|验证码|登录|登陆|sign[ -]?in|login", lowered):
        signals.append("potential_access_restriction")
    return {
        "page_kind": kind,
        "title": page_title[:200],
        "signals": signals,
        "js_shell": js_shell,
        "visible_text_chars": len(visible),
        "content_type": content_type[:120],
    }


def preflight_url(
    url: str,
    *,
    budget_seconds: float = MAX_PREFLIGHT_SECONDS,
    session: Any | None = None,
) -> dict[str, Any]:
    """Perform an anonymous bounded HTTP probe; HTTP 200 is never crawl success."""
    requested = str(url or "").strip()
    base = {
        "requested_url": _redact_url(requested),
        "final_url": None,
        "redirects": [],
        "http_status": None,
        "preflight_status": "unknown",
        "allow_crawl": False,
        "page_kind": "unknown",
        "reason": "",
    }
    guard_error = _url_guard(requested)
    if guard_error:
        base.update(preflight_status="skip", reason=guard_error)
        return base
    static_reason = _static_destination_reason(requested)
    if static_reason:
        base.update(
            final_url=_redact_url(requested),
            preflight_status="skip",
            page_kind="non_recruitment" if static_reason == "non_recruitment_admin_entry" else "article_or_form",
            reason=static_reason,
        )
        return base
    owned = session is None
    client = session or requests.Session()
    if hasattr(client, "trust_env"):
        client.trust_env = False
    deadline = time.monotonic() + min(MAX_PREFLIGHT_SECONDS, max(0.1, budget_seconds))
    current = requested
    # Fresh anonymous session per company; retain server-set cookies within its redirect chain.
    _clear_cookies(client)
    visited = {(current, tuple(sorted(client.cookies.items())))}
    try:
        for _ in range(MAX_REDIRECTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                base.update(preflight_status="unknown", reason="preflight_timeout")
                return base
            response = None
            try:
                response = client.get(
                    current,
                    allow_redirects=False,
                    stream=True,
                    timeout=max(0.1, remaining),
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                        "User-Agent": "RecruitOps-OfferBiu-Sample/1.0",
                    },
                )
                status_code = int(getattr(response, "status_code", 0) or 0)
                headers = getattr(response, "headers", {}) or {}
                if status_code in REDIRECT_STATUSES:
                    location = str(headers.get("Location") or "").strip()
                    if not location:
                        base.update(
                            final_url=_redact_url(current),
                            http_status=status_code,
                            preflight_status="unknown",
                            reason="redirect_without_location",
                        )
                        return base
                    target = urljoin(current, location)
                    if "#" not in location and urlsplit(current).fragment:
                        target = urlunsplit(urlsplit(target)._replace(fragment=urlsplit(current).fragment))
                    target_error = _url_guard(target)
                    base["redirects"].append({
                        "status": status_code,
                        "from": _redact_url(current),
                        "to": _redact_url(target),
                    })
                    if target_error:
                        base.update(
                            final_url=_redact_url(current),
                            http_status=status_code,
                            preflight_status="skip",
                            reason=f"unsafe_redirect:{target_error}",
                        )
                        return base
                    state = (target, tuple(sorted(client.cookies.items())))
                    if state in visited:
                        base.update(
                            final_url=_redact_url(current),
                            http_status=status_code,
                            preflight_status="skip",
                            reason="redirect_loop",
                        )
                        return base
                    visited.add(state)
                    current = target
                    continue
                body, truncated = _read_body(response)
                content_type = str(headers.get("Content-Type") or "")
                encoding = getattr(response, "encoding", None) or "utf-8"
                decoded = body.decode(encoding, "replace")
                title_match = re.search(r"<title[^>]*>(.*?)</title>", decoded, re.I | re.S)
                title = BeautifulSoup(
                    title_match.group(0), "html.parser"
                ).get_text(" ", strip=True) if title_match else ""
                observation = _page_observation(
                    current, status_code, body, title, content_type,
                )
                base.update(
                    final_url=_redact_url(current),
                    http_status=status_code,
                    body_bytes=len(body),
                    body_truncated=truncated,
                    **observation,
                )
                if observation["page_kind"] == "article_or_form":
                    base.update(preflight_status="skip", reason=observation["signals"][0])
                elif observation["page_kind"] == "login_or_captcha":
                    base.update(preflight_status="skip", reason="access_restricted")
                elif status_code in RESTRICTED_STATUSES:
                    base.update(preflight_status="skip", reason=f"http_{status_code}")
                elif status_code >= 400:
                    base.update(preflight_status="unknown", allow_crawl=True, reason=f"http_{status_code}")
                else:
                    base.update(
                        preflight_status="checked",
                        allow_crawl=True,
                        reason="bounded_http_probe_only",
                    )
                return base
            except (requests.RequestException, OSError, ValueError) as exc:
                base.update(
                    final_url=_redact_url(current),
                    preflight_status="unknown",
                    allow_crawl=True,
                    reason=f"{type(exc).__name__}: {str(exc)[:240]}",
                )
                return base
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
        base.update(
            final_url=_redact_url(current),
            preflight_status="unknown",
            allow_crawl=False,
            reason="redirect_limit",
        )
        return base
    finally:
        _clear_cookies(client)
        if owned:
            close = getattr(client, "close", None)
            if callable(close):
                close()


def _diagnosis_payload(diagnosis: Any) -> dict[str, Any]:
    dump = getattr(diagnosis, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    return {
        "url": getattr(diagnosis, "url", ""),
        "entry_kind": getattr(diagnosis, "entry_kind", ""),
        "crawler_key": getattr(diagnosis, "crawler_key", None),
        "candidate_kind": getattr(diagnosis, "candidate_kind", None),
        "reason": getattr(diagnosis, "reason", ""),
    }


class _ProcessAdapter:
    def __init__(self, process: mp.Process) -> None:
        self.process = process

    @property
    def pid(self) -> int | None:
        return self.process.pid

    def poll(self) -> int | None:
        return None if self.process.is_alive() else self.process.exitcode

    def kill(self) -> None:
        self.process.kill()

    def wait(self, timeout: float | None = None) -> int | None:
        self.process.join(timeout)
        if self.process.is_alive():
            raise subprocess.TimeoutExpired("offerbiu-crawler", timeout)
        return self.process.exitcode


def _crawler_child(output: Any, kwargs: dict[str, Any]) -> None:
    try:
        value = SubprocessCandidateCrawlerProcess()(**kwargs)
        output.put({"ok": True, "value": value})
    except BaseException as exc:  # child boundary: serialize all failures
        output.put({
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[-1_000:],
        })
    finally:
        output.close()


def _terminate_child_tree(child: mp.Process) -> None:
    from packages.pipeline import isolation

    try:
        isolation._terminate_process_tree(_ProcessAdapter(child))
    except Exception:
        if child.is_alive():
            child.kill()
        child.join(timeout=2)


def _isolated_process_call(
    *,
    company: str,
    crawler_key: str,
    source_url: str,
    timeout_seconds: float,
    source_context: dict[str, Any] | None,
) -> dict[str, Any] | list[dict[str, Any]]:
    kwargs = {
        "company": company,
        "crawler_key": crawler_key,
        "source_url": source_url,
        "timeout_seconds": timeout_seconds,
        "source_context": source_context,
    }
    context = mp.get_context("spawn")
    output = context.Queue(maxsize=1)
    child = context.Process(target=_crawler_child, args=(output, kwargs))
    child.start()
    timed_out = False
    try:
        wait_seconds = max(0.1, timeout_seconds - PROCESS_CLEANUP_RESERVE_SECONDS)
        try:
            payload = output.get(timeout=wait_seconds)
        except Empty as exc:
            timed_out = True
            _terminate_child_tree(child)
            raise TimeoutError(
                f"crawler exceeded hard timeout of {timeout_seconds:g}s"
            ) from exc
        if not payload.get("ok"):
            raise RuntimeError(
                f"{payload.get('error_type', 'crawler_error')}: {payload.get('error', '')}"
            )
        return payload["value"]
    finally:
        if child.is_alive():
            if not timed_out:
                child.join(timeout=1)
            if child.is_alive():
                _terminate_child_tree(child)
        else:
            child.join(timeout=0)
        output.close()
        output.cancel_join_thread()


def call_crawler_process(
    company: str,
    crawler_key: str,
    source_url: str,
    timeout_seconds: float,
    source_context: dict[str, Any] | None = None,
    *,
    process: Callable[..., Any] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Call the existing process directly; production calls it in a killable child."""
    if process is not None:
        return process(
            company=company,
            crawler_key=crawler_key,
            source_url=source_url,
            timeout_seconds=timeout_seconds,
            source_context=source_context,
        )
    return _isolated_process_call(
        company=company,
        crawler_key=crawler_key,
        source_url=source_url,
        timeout_seconds=timeout_seconds,
        source_context=source_context,
    )


def _crawl_stats(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        result: dict[str, Any] = {
            "jobs": value,
            "pagination_complete": None,
            "completeness_known": None,
            "pages_seen": None,
            "total_pages": None,
            "has_more": None,
            "advertised_total": None,
            "termination_reasons": ["list_result_without_pagination_evidence"],
        }
    elif isinstance(value, Mapping):
        result = dict(value)
    else:
        raise ValueError("crawler result must be a list or object")
    raw_jobs = result.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("crawler result jobs must be a list")
    valid_jobs = [dict(job) for job in raw_jobs if isinstance(job, Mapping)]
    complete = 0
    incomplete = 0
    unknown = 0
    for job in valid_jobs:
        try:
            assessment = assess_jd_capture(job)
            if assessment.complete:
                complete += 1
            elif not str(job.get("jd_raw") or "").strip():
                incomplete += 1
            elif assessment.reason_code in {"capture_unverified", "identity_unverified"}:
                unknown += 1
            else:
                incomplete += 1
        except Exception:
            unknown += 1
    return {
        "raw_job_count": len(raw_jobs),
        "jd_check": "official_capture_evidence_v1",
        "valid_raw_job_count": len(valid_jobs),
        "invalid_raw_job_count": len(raw_jobs) - len(valid_jobs),
        "complete_jd_count": complete,
        "incomplete_jd_count": incomplete,
        "unknown_jd_count": unknown,
        "jd_complete_rate": round(complete / len(valid_jobs), 4) if valid_jobs else None,
        "pagination_evidence": {
            key: result.get(key)
            for key in (
                "pagination_complete", "completeness_known", "pages_seen",
                "total_pages", "has_more", "advertised_total",
            )
        },
        "termination_reasons": [str(item) for item in result.get("termination_reasons") or []],
        "effective_source_urls": [
            _redact_url(item) for item in result.get("effective_source_urls") or []
        ],
        "source_runs": result.get("source_runs") or [],
        "raw_jobs": raw_jobs,
        "raw_result_error_code": result.get("error_code"),
    }


def _apply_urls(value: object) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    return [str(item).strip() for item in values if str(item or "").strip()]


def _skip_result(
    row: Mapping[str, Any],
    *,
    company: str,
    evidence: dict[str, Any],
    preflight: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "sample_id": row.get("id"),
        "company": company,
        "source": "offerbiu",
        "read_only": True,
        "raw_sample": _safe_sample(row),
        "selection": row.get("_selection"),
        "source_cohort_evidence": evidence,
        "preflight": preflight,
        "crawl": None,
        "crawler_status": "skipped",
        "skip_reason": reason,
        "formal_acceptance": "not_run",
        "model_calls": 0,
        "db_writes": 0,
    }


def evaluate_sample(
    row: Mapping[str, Any],
    *,
    timeout_seconds: float = 60.0,
    preflight: Callable[..., dict[str, Any]] = preflight_url,
    process: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    company = str(row.get("companyName") or "").strip()
    urls = _apply_urls(row.get("applyUrl"))
    source_url = urls[0] if urls else ""
    evidence = {
        "source": "offerbiu",
        "sample_id": row.get("id"),
        "company_name": company,
        "apply_url": _redact_url(source_url),
        "industry_group_codes": row.get("industryGroupCodes"),
        "selection": row.get("_selection"),
    }
    if not source_url:
        return _skip_result(
            row, company=company, evidence=evidence, preflight=None, reason="missing_apply_url",
        )
    started = time.monotonic()
    try:
        probe = preflight(
            source_url,
            budget_seconds=min(MAX_PREFLIGHT_SECONDS, max(0.1, timeout_seconds)),
        )
    except Exception as exc:
        probe = {
            "preflight_status": "unknown",
            "allow_crawl": True,
            "final_url": _redact_url(source_url),
            "page_kind": "unknown",
            "reason": f"probe_error:{type(exc).__name__}",
        }
    if probe.get("preflight_status") == "skip" or not probe.get("allow_crawl", False):
        return _skip_result(
            row,
            company=company,
            evidence=evidence,
            preflight=probe,
            reason=str(probe.get("reason") or "preflight_restricted"),
        )
    crawl_url = str(probe.get("final_url") or source_url)
    if crawl_url.startswith("<"):
        crawl_url = source_url
    guard_error = _url_guard(crawl_url)
    if guard_error:
        return _skip_result(
            row, company=company, evidence=evidence, preflight=probe, reason=guard_error,
        )
    diagnosis = diagnose_candidate_entry(crawl_url)
    if diagnosis.entry_kind in {"invalid_entry", "form_application"}:
        return _skip_result(
            row,
            company=company,
            evidence=evidence,
            preflight=probe,
            reason=diagnosis.reason,
        )
    crawler_key = diagnosis.crawler_key or "render"
    remaining = max(0.1, timeout_seconds - (time.monotonic() - started))
    context = {
        "source_cohort_source": "offerbiu",
        "source_cohort_evidence": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        "source_cohort_url": _redact_url(source_url),
    }
    try:
        raw_result = call_crawler_process(
            company,
            crawler_key,
            crawl_url,
            remaining,
            context,
            process=process,
        )
        crawl = _crawl_stats(raw_result)
        crawler_status = (
            "raw_jobs_observed"
            if crawl["raw_job_count"]
            else "empty_raw_result_not_proof_of_no_jobs"
        )
        return {
            "sample_id": row.get("id"),
            "company": company,
            "source": "offerbiu",
            "read_only": True,
            "raw_sample": _safe_sample(row),
            "selection": row.get("_selection"),
            "source_cohort_evidence": evidence,
            "preflight": probe,
            "diagnosis": _diagnosis_payload(diagnosis),
            "crawler_key": crawler_key,
            "crawl_url": _redact_url(crawl_url),
            "crawl": crawl,
            "crawler_status": crawler_status,
            "formal_acceptance": "not_run",
            "model_calls": 0,
            "db_writes": 0,
        }
    except TimeoutError as exc:
        error_code = "timeout"
        error_message = str(exc)
    except Exception as exc:
        error_code = type(exc).__name__
        error_message = str(exc)[-1_000:]
    return {
        "sample_id": row.get("id"),
        "company": company,
        "source": "offerbiu",
        "read_only": True,
        "raw_sample": _safe_sample(row),
        "selection": row.get("_selection"),
        "source_cohort_evidence": evidence,
        "preflight": probe,
        "diagnosis": _diagnosis_payload(diagnosis),
        "crawler_key": crawler_key,
        "crawl_url": _redact_url(crawl_url),
        "crawl": None,
        "crawler_status": "error",
        "error_code": error_code,
        "error_message": error_message,
        "formal_acceptance": "not_run",
        "model_calls": 0,
        "db_writes": 0,
    }


def _company_aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    crawled = [item for item in results if item.get("crawl")]
    raw_jobs = sum(int(item["crawl"].get("raw_job_count") or 0) for item in crawled)
    complete = sum(int(item["crawl"].get("complete_jd_count") or 0) for item in crawled)
    incomplete = sum(int(item["crawl"].get("incomplete_jd_count") or 0) for item in crawled)
    unknown = sum(int(item["crawl"].get("unknown_jd_count") or 0) for item in crawled)
    if any(item.get("crawler_status") == "raw_jobs_observed" for item in results):
        status = "raw_jobs_observed"
    elif any(item.get("crawler_status") == "error" for item in results):
        status = "error_or_partial"
    elif crawled:
        status = "empty_raw_result_not_proof_of_no_jobs"
    else:
        status = "skipped"
    return {
        "sample_count": len(results),
        "raw_job_count": raw_jobs,
        "complete_jd_count": complete,
        "incomplete_jd_count": incomplete,
        "unknown_jd_count": unknown,
        "jd_check": "official_capture_evidence_v1",
        "jd_complete_rate": round(complete / (complete + incomplete + unknown), 4)
        if complete + incomplete + unknown else None,
        "skipped_count": sum(item.get("crawler_status") == "skipped" for item in results),
        "error_count": sum(item.get("crawler_status") == "error" for item in results),
        "status": status,
        "formal_acceptance": "not_run",
        "model_calls": 0,
        "db_writes": 0,
    }


def evaluate_company(
    company: str,
    rows: list[Mapping[str, Any]],
    *,
    timeout_seconds: float = 60.0,
    preflight: Callable[..., dict[str, Any]] = preflight_url,
    process: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    results: list[dict[str, Any]] = []
    for row in rows:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result = _skip_result(
                row,
                company=company,
                evidence={
                    "source": "offerbiu",
                    "sample_id": row.get("id"),
                    "company_name": company,
                    "apply_url": _redact_url(row.get("applyUrl")),
                    "industry_group_codes": row.get("industryGroupCodes"),
                },
                preflight=None,
                reason="company_timeout_before_sample",
            )
        else:
            result = evaluate_sample(
                row,
                timeout_seconds=remaining,
                preflight=preflight,
                process=process,
            )
        results.append(result)
    return {
        "source": "offerbiu",
        "company": company,
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": timeout_seconds,
        "samples": results,
        "aggregate": _company_aggregate(results),
    }


def _pick(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _canonical_record(
    record: Mapping[str, Any],
    *,
    parent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    parent = parent or {}
    raw = record.get("raw_record")
    raw_record = dict(raw) if isinstance(raw, Mapping) else dict(record)
    company = _pick(
        raw_record, "companyName", "company_name", "company",
    ) or _pick(parent, "companyName", "company_name", "company") or _pick(
        record, "companyName", "company_name", "company",
    )
    apply_url = _pick(raw_record, "applyUrl", "apply_url", "normalized_apply_url")
    if apply_url is None:
        apply_url = _pick(record, "applyUrl", "apply_url", "normalized_apply_url")
    if apply_url is None:
        apply_url = _pick(parent, "applyUrl", "apply_url", "normalized_apply_url")
    codes = _pick(raw_record, "industryGroupCodes", "industry_group_codes")
    if codes is None:
        codes = _pick(record, "industryGroupCodes", "industry_group_codes")
    if codes is None:
        codes = _pick(parent, "industryGroupCodes", "industry_group_codes")
    sample = dict(raw_record)
    sample.update(
        {
            "companyName": company,
            "applyUrl": apply_url,
            "industryGroupCodes": codes or [],
            "id": _pick(raw_record, "id") or _pick(record, "id") or _pick(parent, "id"),
            "raw_record": raw_record,
        }
    )
    company_key = _pick(record, "company_key", "companyKey") or _pick(
        parent, "company_key", "companyKey"
    )
    company_key = str(company_key or company or sample["id"] or "unknown-company").strip().casefold()
    sample["_company_key"] = company_key
    return sample


def _candidate_rank(row: Mapping[str, Any]) -> tuple[int, str]:
    classification = str(row.get("classification") or "").casefold()
    family = str(row.get("url_family") or "").casefold()
    url = _apply_urls(row.get("applyUrl"))
    if not url:
        rank = 5
    elif classification in {"wechat_article", "form", "missing"}:
        rank = 4
    elif family == "known_ats":
        rank = 0
    elif classification == "official_or_unknown":
        rank = 1
    elif family == "navigation":
        rank = 2
    else:
        rank = 3
    return rank, _redact_url(url[0] if url else "")


def _select_representatives(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["_company_key"])].append(row)
    selected: list[dict[str, Any]] = []
    for company_key, candidates in grouped.items():
        ordered = sorted(candidates, key=_candidate_rank)
        choice = dict(ordered[0])
        selected_url = _apply_urls(choice.get("applyUrl"))
        classification = str(choice.get("classification") or "").casefold()
        if len(ordered) == 1:
            reason = "single_entry"
        elif classification in {"wechat_article", "form", "missing"}:
            reason = "only_static_or_missing_entries_available"
        elif _candidate_rank(choice)[0] == 0:
            reason = "known_ats_entry_preferred"
        else:
            reason = "crawlable_unknown_entry_preferred"
        choice["_selection"] = {
            "representative_only": True,
            "selected_url": _redact_url(selected_url[0] if selected_url else ""),
            "selection_reason": reason,
            "candidate_count": len(ordered),
            "other_entry_count": max(0, len(ordered) - 1),
            "other_entries_verified": False,
            "other_entry_urls_not_tested": [
                _redact_url(url)
                for row in ordered[1:]
                for url in _apply_urls(row.get("applyUrl"))
            ],
        }
        selected.append(choice)
    return selected


def _limit_distinct_company_names(
    rows: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("companyName") or "").strip()
        key = name.casefold() if name else str(row.get("_company_key") or "")
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def load_samples(path: Path) -> list[dict[str, Any]]:
    """Load legacy samples or the link-quality selected_companies payload.

    The returned rows are one representative entry per company. Other entries
    remain named in selection metadata and are explicitly not treated as tested.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        raw_rows = [
            _canonical_record(row)
            for row in payload
            if isinstance(row, Mapping)
        ]
    elif isinstance(payload, Mapping):
        companies = payload.get("selected_companies")
        if isinstance(companies, list):
            for company in companies:
                if not isinstance(company, Mapping):
                    continue
                records = company.get("records")
                if isinstance(records, list):
                    raw_rows.extend(
                        _canonical_record(record, parent=company)
                        for record in records
                        if isinstance(record, Mapping)
                    )
        if not raw_rows:
            records = payload.get("selected_records")
            if not isinstance(records, list):
                records = payload.get("samples")
            if isinstance(records, list):
                raw_rows = [
                    _canonical_record(record)
                    for record in records
                    if isinstance(record, Mapping)
                ]
    else:
        raise ValueError(
            "samples input must be a list or an object with selected_companies, "
            "selected_records, or samples"
        )
    return _select_representatives(raw_rows)


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "company"
    return text[:60]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_report(path: Path, company_payloads: list[dict[str, Any]]) -> None:
    lines = [
        "# OfferBiu crawl sample",
        "",
        "- Read-only: yes",
        "- Model calls: 0",
        "- Database writes: 0",
        "- Formal crawler acceptance: not run",
        "",
        "| Company | Status | Raw jobs | Complete JD | Incomplete JD |",
        "|---|---|---:|---:|---:|",
    ]
    for payload in company_payloads:
        item = payload["aggregate"]
        values = (
            payload["company"],
            item["status"],
            item["raw_job_count"],
            item["complete_jd_count"],
            item["incomplete_jd_count"],
        )
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in values) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--company", action="append", default=[], help="Restrict a targeted retest to exact company names")
    args = parser.parse_args(argv)
    workers = min(3, max(1, args.workers))
    timeout = min(MAX_TIMEOUT_SECONDS, max(1.0, args.timeout))
    limit = max(0, args.limit)
    rows = load_samples(args.samples)
    if args.company:
        rows = [row for row in rows if row.get("companyName") in set(args.company)]
    rows = _limit_distinct_company_names(rows, limit)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("companyName") or "").strip() or "unknown-company"].append(row)
    output_dir = args.output_dir
    checkpoint_dir = output_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="offerbiu") as executor:
        futures = {
            executor.submit(evaluate_company, company, company_rows, timeout_seconds=timeout): company
            for company, company_rows in grouped.items()
        }
        for future in as_completed(futures):
            company = futures[future]
            try:
                payload = future.result()
            except Exception as exc:
                payload = {
                    "source": "offerbiu",
                    "company": company,
                    "read_only": True,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "timeout_seconds": timeout,
                    "samples": [],
                    "aggregate": {
                        "sample_count": len(grouped[company]),
                        "raw_job_count": 0,
                        "complete_jd_count": 0,
                        "incomplete_jd_count": 0,
                        "jd_complete_rate": None,
                        "skipped_count": 0,
                        "error_count": len(grouped[company]),
                        "status": "error_or_partial",
                        "error": f"{type(exc).__name__}: {str(exc)[-500:]}",
                        "formal_acceptance": "not_run",
                        "model_calls": 0,
                        "db_writes": 0,
                    },
                }
            payloads[company] = payload
            digest = hashlib.sha1(company.encode("utf-8")).hexdigest()[:10]
            _write_json(checkpoint_dir / f"{_slug(company)}-{digest}.json", payload)
    ordered = [payloads[key] for key in grouped if key in payloads]
    summary = {
        "source": "offerbiu",
        "read_only": True,
        "input": str(args.samples.resolve()),
        "selected_sample_count": len(rows),
        "company_count": len(ordered),
        "workers": workers,
        "timeout_seconds_per_company": timeout,
        "limit": limit,
        "companies": [
            {
                "company": payload["company"],
                "status": payload["aggregate"]["status"],
                "raw_job_count": payload["aggregate"]["raw_job_count"],
                "complete_jd_count": payload["aggregate"]["complete_jd_count"],
                "incomplete_jd_count": payload["aggregate"]["incomplete_jd_count"],
            }
            for payload in ordered
        ],
        "formal_acceptance": "not_run",
        "model_calls": 0,
        "db_writes": 0,
    }
    _write_json(output_dir / "summary.json", summary)
    _write_report(output_dir / "report.md", ordered)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
