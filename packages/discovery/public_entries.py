"""Bounded public search for recruitment-entry candidates.

Search results are untrusted discovery hints.  They never prove that a URL is
official, current, complete, or safe to persist; the crawler acceptance path
must establish those properties separately.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol
from urllib.parse import parse_qs, quote_plus, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from packages.recruitment_core.entry import diagnose_candidate_entry
from packages.recruitment_core.crawlers.render import render_page


MAX_COMPANIES = 6
MAX_QUERIES_PER_COMPANY = 2
MAX_CANDIDATES_PER_COMPANY = 5

_SEARCH_HOSTS = (
    "bing.com", "google.com", "baidu.com", "duckduckgo.com", "brave.com", "sogou.com",
)
_AGGREGATOR_HOSTS = (
    "nowcoder.com", "kanzhun.com", "zhipin.com", "liepin.com",
    "jobui.com", "shixiseng.com", "yingjiesheng.com",
)
_SCHOOL_HOST_SUFFIXES = (
    "edu.cn", "ac.cn", "edu", "ac.uk",
)
_COMPANY_SUFFIXES = re.compile(
    r"(?:研发中心|研究中心|制造部|事业部|研究院|集团|股份|有限责任|有限|科技|技术|信息|网络|电子|软件|智能|公司)+$",
)
_RECRUITMENT_RE = re.compile(
    r"校园招聘|校招|应届|毕业生|招聘职位|招聘岗位|campus|graduate|career|jobs?|join",
    re.I,
)
_SCHOOL_PAGE_RE = re.compile(
    r"(?:就业信息网|就业中心|就业服务|毕业生就业|高校就业|大学就业|学院就业|"
    r"校园招聘会|校园宣讲会|双选会)",
    re.I,
)
_CURRENT_COHORT_RE = re.compile(r"2027|27届|二七届")


@dataclass(frozen=True)
class PublicSearchHit:
    provider: str
    query: str
    url: str
    title: str
    snippet: str


@dataclass(frozen=True)
class RankedEntryCandidate:
    hit: PublicSearchHit
    score: int
    entry_kind: str
    crawler_key: str | None
    company_evidence: str


@dataclass(frozen=True)
class EntryIdentityEvidence:
    valid: bool
    source_url: str
    company_evidence: str = ""
    recruitment_evidence: str = ""
    reason: str = ""


class PublicSearchError(RuntimeError):
    pass


class PublicSearchProvider(Protocol):
    name: str

    def search(self, query: str, timeout_seconds: float) -> list[PublicSearchHit]: ...


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold().rstrip(".")


def _is_host_or_subdomain(hostname: str, expected: str) -> bool:
    return hostname == expected or hostname.endswith(f".{expected}")


def _is_school_host(hostname: str) -> bool:
    return any(
        _is_host_or_subdomain(hostname, suffix)
        for suffix in _SCHOOL_HOST_SUFFIXES
    )


def _decode_bing_target(value: str) -> str | None:
    """Decode Bing's ``u=a1<base64>`` redirect without following it."""

    parsed = urlsplit(value)
    if not _is_host_or_subdomain((parsed.hostname or "").casefold(), "bing.com"):
        return value
    encoded = (parse_qs(parsed.query).get("u") or [""])[0]
    if not encoded.startswith("a1"):
        return None
    payload = encoded[2:]
    try:
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    return decoded if urlsplit(decoded).scheme in {"http", "https"} else None


def normalize_search_result_url(value: str) -> str | None:
    target = _decode_bing_target(value.strip())
    if not target:
        return None
    try:
        parsed = urlsplit(target)
        parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    if any(_is_host_or_subdomain(hostname, expected) for expected in _SEARCH_HOSTS):
        return None
    if re.search(r"\.(?:avif|gif|jpe?g|png|svg|webp)(?:$|\?)", parsed.path, re.I):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


class BingHtmlSearchProvider:
    name = "bing_html"

    def __init__(self, *, session: requests.Session | None = None):
        self._session = session or requests.Session()

    def search(self, query: str, timeout_seconds: float) -> list[PublicSearchHit]:
        try:
            response = self._session.get(
                f"https://www.bing.com/search?q={quote_plus(query)}",
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                },
                timeout=max(1.0, timeout_seconds),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PublicSearchError(f"Bing search failed: {exc}") from exc
        soup = BeautifulSoup(response.text, "html.parser")
        hits: list[PublicSearchHit] = []
        for node in soup.select("li.b_algo")[:20]:
            anchor = node.select_one("h2 a[href]")
            if anchor is None:
                continue
            url = normalize_search_result_url(str(anchor.get("href") or ""))
            if not url:
                continue
            caption = node.select_one(".b_caption p")
            hits.append(PublicSearchHit(
                provider=self.name,
                query=query,
                url=url,
                title=anchor.get_text(" ", strip=True)[:300],
                snippet=(caption.get_text(" ", strip=True) if caption else "")[:1_000],
            ))
        return hits


class JinaBaiduSearchProvider:
    """Read Baidu's public result text through Jina Reader.

    The provider extracts only literal external URLs printed in result text. It
    does not follow Baidu redirects, images, or search-engine links.
    """

    name = "jina_baidu"

    def __init__(self, *, session: requests.Session | None = None):
        self._session = session or requests.Session()

    def search(self, query: str, timeout_seconds: float) -> list[PublicSearchHit]:
        url = f"https://r.jina.ai/http://www.baidu.com/s?wd={quote_plus(query)}"
        try:
            response = self._session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
                timeout=max(1.0, timeout_seconds),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PublicSearchError(f"Jina/Baidu search failed: {exc}") from exc
        lines = response.text.splitlines()
        hits: list[PublicSearchHit] = []
        seen: set[str] = set()
        for index, line in enumerate(lines):
            for raw_url in re.findall(r"https?://[^\s<>()\[\]{}]+", line):
                normalized = normalize_search_result_url(raw_url.rstrip(".,;:，。；：'\""))
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                context = " ".join(
                    item.strip()
                    for item in lines[max(0, index - 1): min(len(lines), index + 2)]
                    if item.strip()
                )
                hits.append(PublicSearchHit(
                    provider=self.name,
                    query=query,
                    url=normalized,
                    title=line.strip()[:300],
                    snippet=context[:1_000],
                ))
        return hits


class DefaultPublicSearchProvider:
    name = "jina_baidu_then_bing"

    def __init__(
        self,
        providers: tuple[PublicSearchProvider, ...] | None = None,
    ):
        self._providers = providers or (
            JinaBaiduSearchProvider(),
            BingHtmlSearchProvider(),
        )

    def search(self, query: str, timeout_seconds: float) -> list[PublicSearchHit]:
        started = perf_counter()
        failures: list[str] = []
        collected: list[PublicSearchHit] = []
        for index, provider in enumerate(self._providers):
            remaining = timeout_seconds - (perf_counter() - started)
            if remaining <= 0:
                break
            providers_left = len(self._providers) - index
            provider_budget = max(1.0, remaining / providers_left)
            try:
                hits = provider.search(query, provider_budget)
            except PublicSearchError as exc:
                failures.append(str(exc))
                continue
            collected.extend(hits)
        if collected:
            return collected
        if failures:
            raise PublicSearchError("; ".join(failures)[-1_000:])
        return []


def build_company_queries(company_name: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", " ", company_name).strip()
    compact = re.sub(r"[\s·・()（）\-_/]+", "", normalized).casefold()
    stem = _COMPANY_SUFFIXES.sub("", compact)
    search_name = stem if len(stem) >= 3 else normalized
    return (
        f"{normalized} 校园招聘 官网",
        f"{search_name} 2027 校园招聘",
    )


def _company_terms(company_name: str) -> tuple[str, ...]:
    compact = re.sub(r"[\s·・()（）\-_/]+", "", company_name).casefold()
    stem = _COMPANY_SUFFIXES.sub("", compact)
    terms = [term for term in (compact, stem) if len(term) >= 2]
    return tuple(dict.fromkeys(terms))


def observe_public_entry_identity(
    company_name: str,
    source_url: str,
    *,
    timeout_seconds: float = 30.0,
    render=render_page,
    http_get=None,
) -> EntryIdentityEvidence:
    """Require company and recruitment evidence from the candidate page itself."""

    normalized = normalize_search_result_url(source_url)
    if not normalized:
        return EntryIdentityEvidence(False, source_url, reason="invalid_or_search_url")
    if _is_school_host(_hostname(normalized)):
        return EntryIdentityEvidence(False, normalized, reason="third_party_school_host")
    diagnosis = diagnose_candidate_entry(normalized)
    if diagnosis.entry_kind in {"invalid_entry", "form_application"}:
        return EntryIdentityEvidence(False, normalized, reason=diagnosis.reason)
    html = ""
    try:
        html = str(render(
            normalized,
            timeout_ms=max(1, int(timeout_seconds * 1_000)),
            extra_wait_ms=1_500,
            scroll_times=1,
        ) or "")
    except (OSError, RuntimeError, TimeoutError):
        html = ""
    if not html and http_get is not None:
        try:
            response = http_get(
                normalized,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9"},
                timeout=max(1.0, timeout_seconds),
            )
            response.raise_for_status()
            html = str(response.text or "")
        except requests.RequestException:
            html = ""
    if not html:
        return EntryIdentityEvidence(False, normalized, reason="page_unavailable")
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript, template"):
        node.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())[:200_000]
    matched = next((term for term in _company_terms(company_name) if term in text.casefold()), "")
    if not matched:
        return EntryIdentityEvidence(False, normalized, reason="company_identity_not_observed")
    recruitment = _RECRUITMENT_RE.search(text)
    if recruitment is None:
        return EntryIdentityEvidence(
            False,
            normalized,
            company_evidence=matched,
            reason="recruitment_content_not_observed",
        )
    start = max(0, recruitment.start() - 80)
    end = min(len(text), recruitment.end() + 120)
    return EntryIdentityEvidence(
        True,
        normalized,
        company_evidence=matched,
        recruitment_evidence=text[start:end],
        reason="page_identity_verified",
    )


def rank_entry_candidate(company_name: str, hit: PublicSearchHit) -> RankedEntryCandidate | None:
    url = normalize_search_result_url(hit.url)
    if not url:
        return None
    host = _hostname(url)
    if (
        any(_is_host_or_subdomain(host, item) for item in _AGGREGATOR_HOSTS)
        or _is_school_host(host)
        or _SCHOOL_PAGE_RE.search(f"{hit.title} {hit.snippet}")
    ):
        return None
    diagnosis = diagnose_candidate_entry(url)
    if diagnosis.entry_kind in {"invalid_entry", "form_application"}:
        return None
    haystack = f"{hit.title} {hit.snippet} {url}".casefold()
    matched = next((term for term in _company_terms(company_name) if term in haystack), None)
    if not matched:
        return None
    if not _RECRUITMENT_RE.search(haystack):
        return None
    score = 50
    if _CURRENT_COHORT_RE.search(haystack):
        score += 15
    if re.search(r"官网|官方", haystack):
        score += 10
    if diagnosis.entry_kind == "existing_adapter":
        score += 20
    elif diagnosis.entry_kind == "declarative_candidate":
        score += 10
    if re.search(r"大学|学院|媒体|新闻|公告", hit.title, re.I):
        score -= 20
    return RankedEntryCandidate(
        hit=PublicSearchHit(
            provider=hit.provider,
            query=hit.query,
            url=url,
            title=hit.title,
            snippet=hit.snippet,
        ),
        score=score,
        entry_kind=diagnosis.entry_kind,
        crawler_key=diagnosis.crawler_key,
        company_evidence=matched,
    )


def discover_company_entry_candidates(
    company_name: str,
    *,
    provider: PublicSearchProvider | None = None,
    timeout_seconds: float = 30.0,
    max_queries: int = MAX_QUERIES_PER_COMPANY,
    max_candidates: int = MAX_CANDIDATES_PER_COMPANY,
) -> tuple[list[str], list[RankedEntryCandidate]]:
    """Return bounded, ranked candidate URLs with their search provenance."""

    provider = provider or DefaultPublicSearchProvider()
    deadline = perf_counter() + max(1.0, timeout_seconds)
    queries = list(build_company_queries(company_name))[:max(1, min(max_queries, MAX_QUERIES_PER_COMPANY))]
    ranked: dict[str, RankedEntryCandidate] = {}
    attempted: list[str] = []
    for query in queries:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            break
        attempted.append(query)
        for hit in provider.search(query, remaining):
            candidate = rank_entry_candidate(company_name, hit)
            if candidate is None:
                continue
            current = ranked.get(candidate.hit.url)
            if current is None or candidate.score > current.score:
                ranked[candidate.hit.url] = candidate
    ordered = sorted(
        ranked.values(),
        key=lambda item: (-item.score, item.hit.url),
    )[:max(1, min(max_candidates, MAX_CANDIDATES_PER_COMPANY))]
    return attempted, ordered


__all__ = [
    "BingHtmlSearchProvider",
    "DefaultPublicSearchProvider",
    "EntryIdentityEvidence",
    "JinaBaiduSearchProvider",
    "MAX_CANDIDATES_PER_COMPANY",
    "MAX_COMPANIES",
    "MAX_QUERIES_PER_COMPANY",
    "PublicSearchError",
    "PublicSearchHit",
    "PublicSearchProvider",
    "RankedEntryCandidate",
    "build_company_queries",
    "discover_company_entry_candidates",
    "normalize_search_result_url",
    "observe_public_entry_identity",
    "rank_entry_candidate",
]
