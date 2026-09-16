"""Evidence-based campus recruitment cohort classification.

Only jobs with explicit official evidence for the current cohort may enter JD
hydration or AI analysis. Ambiguous campus roles remain visible for manual
verification without spending model tokens.
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from .crawlers.render import render_page


CURRENT_COHORT = 2027
UNKNOWN_COHORT = 0

_COHORT_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2}|[12]\d)\s*届", re.I),
    re.compile(
        r"(?<!\d)(20\d{2}|[12]\d)\s*(?:年)?\s*"
        r"(?:春招|秋招|校招|校园招聘|应届生招聘)",
        re.I,
    ),
    re.compile(
        r"(?<!\d)(20\d{2}|[12]\d)\s*年?\s*"
        r"(?:应届(?:毕业生?)?|毕业生)",
        re.I,
    ),
    re.compile(
        r"(?:campus(?:\s+recruitment)?|new\s*grad(?:uate)?)"
        r"[\s_/-]*(20\d{2})(?!\d)",
        re.I,
    ),
    re.compile(
        r"(?<!\d)(20\d{2})[\s_/-]*"
        r"(?:campus(?:\s+recruitment)?|new\s*grad(?:uate)?)",
        re.I,
    ),
)
_GRADUATION_RANGE_PATTERNS = (
    re.compile(
        r"毕业时间\s*[:：]?\s*(?:为)?\s*20\d{2}\s*年\s*\d{1,2}\s*月"
        r"(?:\s*\d{1,2}\s*日)?\s*(?:至|到|-|—|~|～)\s*"
        r"(20\d{2})\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?",
        re.I,
    ),
    re.compile(
        r"(?<!\d)20\d{2}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"
        r"\s*(?:至|到|-|—|~|～)\s*(20\d{2})\s*年\s*\d{1,2}\s*月"
        r"(?:\s*\d{1,2}\s*日)?\s*期间毕业",
        re.I,
    ),
)
_RECRUITMENT_CONTEXT = re.compile(
    r"届|春招|秋招|校招|校园招聘|应届生招聘|毕业生|招聘项目|"
    r"campus|new\s*grad",
    re.I,
)
_CAMPAIGN_YEAR = re.compile(r"(?<!\d)(20\d{2}|[12]\d)(?!\d)", re.I)
_CAMPAIGN_MARKER = re.compile(
    r"春招|秋招|校招(?!生)|校园招聘|校园招募|应届生招聘|毕业生招聘|"
    r"campus\s+recruitment|new\s*grad(?:uate)?",
    re.I,
)
_MIXED_TRACK = re.compile(r"实习|提前批|intern(?:ship)?", re.I)
_FORMAL_CAMPAIGN_PATTERNS = (
    re.compile(
        r"(?<!\d)(20\d{2}|[12]\d)\s*(?:届|年)?[^。；;\n\d]{0,20}?"
        r"(?:春招|秋招|校招|校园招聘|校园招募|应届生招聘|毕业生招聘)",
        re.I,
    ),
    re.compile(
        r"(?:春招|秋招|校招|校园招聘|校园招募|应届生招聘|毕业生招聘)"
        r"[^。；;\n\d]{0,20}?(?<!\d)(20\d{2}|[12]\d)(?!\d)",
        re.I,
    ),
    re.compile(
        r"(?:面向|招聘对象)[^。；;\n]{0,20}?"
        r"(?<!\d)(20\d{2}|[12]\d)\s*届(?:高校)?毕业生",
        re.I,
    ),
)
_INTERNSHIP_CAMPAIGN_PATTERNS = (
    re.compile(
        r"(?<!\d)(20\d{2}|[12]\d)\s*(?:届|年)?[^。；;\n]{0,12}?"
        r"(?:实习|intern(?:ship)?)",
        re.I,
    ),
    re.compile(
        r"(?:实习|intern(?:ship)?)[^。；;\n]{0,12}?"
        r"(?<!\d)(20\d{2}|[12]\d)(?!\d)",
        re.I,
    ),
)
_DATE_AFTER_YEAR = re.compile(r"^\s*(?:年?\s*\d{1,2}\s*月|[-/.]\s*\d{1,2})")
_RENDER_SEMAPHORE = threading.BoundedSemaphore(2)
OC_TRUSTED_SOURCE = "OC固定筛选2027届秋招"
_OFFICIAL_CAMPUS_PORTAL_RE = re.compile(
    r"校招官网|校园招聘|校园招募|应届生招聘|毕业生招聘|"
    r"campus\s+recruitment|new\s*grad(?:uate)?",
    re.I,
)


def _normalize_year(raw: str) -> int:
    year = int(raw)
    return year if year >= 2000 else 2000 + year


def years_in_text(text: str) -> set[int]:
    """Return cohort years only when they occur in recruitment context."""
    value = " ".join(str(text or "").split())
    if not value:
        return set()
    years = {
        int(raw)
        for pattern in _GRADUATION_RANGE_PATTERNS
        for raw in pattern.findall(value)
    }
    if not _RECRUITMENT_CONTEXT.search(value):
        return years
    for pattern in _COHORT_PATTERNS:
        years.update(_normalize_year(raw) for raw in pattern.findall(value))
    return years


def _evidence_snippet(text: str, year: int) -> str:
    compact = " ".join(str(text or "").split())
    markers = (str(year), f"{year % 100}届")
    positions = [compact.find(marker) for marker in markers if compact.find(marker) >= 0]
    start = max(0, (min(positions) if positions else 0) - 45)
    return compact[start:start + 150]


def campaign_years_in_text(text: str) -> dict[int, str]:
    """Extract years explicitly tied to a recruitment campaign, not page dates."""
    compact = " ".join(str(text or "").split())
    found: dict[int, str] = {}
    markers = list(_CAMPAIGN_MARKER.finditer(compact))
    for match in _CAMPAIGN_YEAR.finditer(compact):
        if "©" in compact[max(0, match.start() - 4):match.start()]:
            continue
        if _DATE_AFTER_YEAR.match(compact[match.end():match.end() + 12]):
            continue
        if (
            len(match.group(1)) == 2
            and (
                not re.match(r"^\s*届", compact[match.end():match.end() + 4])
                or compact[max(0, match.start() - 1):match.start()] in {"-", "/", "."}
                or compact[max(0, match.start() - 2):match.start()].endswith("月")
                or compact[match.end():match.end() + 1] in {"日", "号"}
            )
        ):
            continue
        nearby_marker = any(
            0 <= marker.start() - match.end() <= 24
            or 0 <= match.start() - marker.end() <= 24
            for marker in markers
        )
        if not nearby_marker:
            continue
        start = max(0, match.start() - 40)
        end = min(len(compact), match.end() + 45)
        context = compact[start:end]
        year = _normalize_year(match.group(1))
        found.setdefault(year, context)
    return found


def _years_from_patterns(text: str, patterns: tuple[re.Pattern, ...]) -> set[int]:
    return {
        _normalize_year(raw)
        for pattern in patterns
        for raw in pattern.findall(text)
    }


def _campaign_decision(text: str, source: str, url: str) -> dict:
    found = campaign_years_in_text(text)
    formal_years = (
        _years_from_patterns(text, _FORMAL_CAMPAIGN_PATTERNS)
        & set(found)
    )
    internship_years = _years_from_patterns(text, _INTERNSHIP_CAMPAIGN_PATTERNS)
    if len(formal_years) == 1:
        year = next(iter(formal_years))
        evidence = found.get(year) or _evidence_snippet(text, year)
        scope = "mixed" if _MIXED_TRACK.search(evidence) else "exclusive"
        if internship_years or _MIXED_TRACK.search(text):
            scope = "formal_with_internships"
        return {
            "cohort": year,
            "cohort_status": "confirmed",
            "cohort_source": source,
            "cohort_evidence": evidence[:500],
            "campaign_scope": scope,
            "campaign_url": url,
        }
    if len(formal_years) > 1:
        return {
            "cohort": UNKNOWN_COHORT,
            "cohort_status": "conflict",
            "cohort_source": f"{source}存在多个招聘届别",
            "cohort_evidence": "；".join(
                f"{year}: {found.get(year) or _evidence_snippet(text, year)}"
                for year in sorted(formal_years)
            )[:500],
            "campaign_scope": "mixed",
            "campaign_url": url,
        }
    if internship_years:
        return unknown_cohort(
            source=f"{source}仅发现实习届别，未发现正式校招届别",
            campaign_url=url,
        )
    return unknown_cohort(source=f"{source}未发现明确届别", campaign_url=url)


def _portal_evidence(text: str, url: str) -> tuple[bool, str]:
    """Return auditable evidence that a destination is an official campus portal."""

    compact = " ".join(str(text or "").split())
    match = _OFFICIAL_CAMPUS_PORTAL_RE.search(compact)
    if match:
        start = max(0, match.start() - 70)
        return True, compact[start:start + 220]

    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.casefold()
    known_campus_route = (
        ((host == "app.mokahr.com" or host.endswith(".mokahr.com")) and "campus" in path)
        or (host.endswith(".zhiye.com") and "/campus" in path)
        or (host.endswith(".hotjob.cn") and "/position/campus" in path)
        or (host.endswith(".jobs.feishu.cn") and "/campus" in path)
        or (host.endswith(".51job.com") and "/campus/" in path)
        or (host == "talent.baidu.com" and path.startswith("/jobs"))
        or (host == "campus.163.com" and path.startswith("/app/job"))
        or (host == "jobs.mihoyo.com" and "/campus" in f"{path}#{urlparse(url).fragment.casefold()}")
        or (host == "career.huawei.com" and "campus" in path)
        or (host == "join.qq.com" and path in {"/", "/index.html", "/post.html"})
        or (host == "jobs.bytedance.com" and "/campus" in path)
        or (host == "zhaopin.meituan.com" and "/campus" in path)
        or (host == "campus.kuaishou.cn" and "/recruit/campus" in path)
        or (host == "campus.jd.com" and "/position" in path)
        or (host == "campus-talent.alibaba.com" and "/campus" in path)
    )
    if known_campus_route:
        return True, f"官方招聘平台校园招聘路由：{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return False, ""


def _attach_portal_evidence(decision: dict, text: str, url: str) -> dict:
    confirmed, evidence = _portal_evidence(text, url)
    return {
        **decision,
        "official_campus_portal": confirmed,
        "portal_evidence": evidence[:500],
    }


def explicit_job_cohort(job: dict) -> dict:
    """Classify a job from official row fields already returned by its crawler."""
    fields = (
        ("title", "岗位标题"),
        ("job_type", "官方招聘类型"),
        ("campaign_text", "官方招聘项目"),
        ("jd_raw", "官方岗位正文"),
    )
    found: dict[int, tuple[str, str]] = {}
    for field, label in fields:
        text = str(job.get(field) or "")
        for year in years_in_text(text):
            found.setdefault(year, (label, _evidence_snippet(text, year)))
    if len(found) == 1:
        year = next(iter(found))
        source, evidence = found[year]
        return {
            "cohort": year,
            "cohort_status": "confirmed",
            "cohort_source": source,
            "cohort_evidence": evidence,
        }
    if len(found) > 1:
        evidence = "；".join(
            f"{year}: {source}“{snippet}”"
            for year, (source, snippet) in sorted(found.items())
        )
        return {
            "cohort": UNKNOWN_COHORT,
            "cohort_status": "conflict",
            "cohort_source": "岗位字段冲突",
            "cohort_evidence": evidence[:500],
        }
    return unknown_cohort()


def inspect_official_campaign(url: str, *, render_fallback: bool = True) -> dict:
    """Inspect one official campus entry page for an explicit cohort statement."""
    if not url:
        return unknown_cohort()
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=12,
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        decision = _attach_portal_evidence(
            _campaign_decision(text, "公司校招入口", url),
            text,
            url,
        )
        if decision["cohort_status"] != "unknown":
            return decision
    except Exception:
        pass
    if render_fallback:
        try:
            with _RENDER_SEMAPHORE:
                rendered = render_page(
                    url,
                    timeout_ms=30000,
                    extra_wait_ms=2500,
                    scroll_times=0,
                )
        except Exception:
            rendered = ""
        if rendered:
            text = BeautifulSoup(rendered, "html.parser").get_text(" ", strip=True)
            return _attach_portal_evidence(
                _campaign_decision(text, "公司校招活动页（渲染）", url),
                text,
                url,
            )
    decision = unknown_cohort(source="公司校招入口未发现明确届别", campaign_url=url)
    return _attach_portal_evidence(decision, "", url)


def unknown_cohort(
    source: str = "岗位与校招入口均未明确届别",
    *,
    campaign_url: str = "",
) -> dict:
    return {
        "cohort": UNKNOWN_COHORT,
        "cohort_status": "unknown",
        "cohort_source": source,
        "cohort_evidence": "",
        "campaign_scope": "unknown",
        "campaign_url": campaign_url,
        "official_campus_portal": False,
        "portal_evidence": "",
    }


def trusted_source_campaign(
    config: dict,
    portal_campaign: dict | None = None,
    *,
    jobs_observed: bool = False,
) -> dict | None:
    """Build a cohort decision from an explicitly trusted discovery source."""
    discovery_source = str(config.get("discovery_source") or "").strip().casefold()
    if discovery_source == "oc_snapshot":
        source = OC_TRUSTED_SOURCE
        year = CURRENT_COHORT
        evidence = str(config.get("source_cohort_evidence") or "").strip() or (
            "OC公司发现快照：按本地策略统一归入2027届"
        )
    else:
        source = str(config.get("source_cohort_source") or "").strip()
        evidence = str(config.get("source_cohort_evidence") or "").strip()
        try:
            year = int(config.get("source_cohort") or 0)
        except (TypeError, ValueError):
            return None
    if year != CURRENT_COHORT or not evidence:
        return None
    if source == "腾讯文档27届秋招":
        source_label = source
        combined_evidence = evidence
        portal_url = str(config.get("source_cohort_url") or "")
    elif source == OC_TRUSTED_SOURCE:
        # OC is the authoritative discovery source for this local project. Its
        # 2027 filter may point at a company homepage or an unlabeled ATS page,
        # so the destination itself does not need to repeat the cohort label.
        # Requiring at least one extracted job keeps dead links and non-job
        # pages from being promoted by provenance alone.
        if not jobs_observed:
            return None
        source_label = "OC 2027届秋招授权来源"
        combined_evidence = f"{evidence}；OC提供地址已实际提取岗位"[:500]
        portal_url = str(
            config.get("source_cohort_url")
            or config.get("careers_url")
            or config.get("campaign_url")
            or ""
        )
    else:
        return None
    return {
        "cohort": year,
        "cohort_status": "confirmed",
        "cohort_source": source_label,
        "cohort_evidence": combined_evidence[:500],
        "campaign_scope": "trusted_source_override",
        "campaign_url": portal_url,
        "official_campus_portal": True,
        "portal_evidence": str((portal_campaign or {}).get("portal_evidence") or "")[:500],
    }


def is_confirmed_current(job: dict) -> bool:
    try:
        year = int(job.get("cohort") or 0)
    except (TypeError, ValueError):
        year = 0
    return (
        year == CURRENT_COHORT
        and str(job.get("cohort_status") or "") == "confirmed"
    )


def annotate_company_jobs(
    jobs: list[dict],
    careers_url: str = "",
    *,
    inspect_page: bool = True,
    campaign: dict | None = None,
) -> list[dict]:
    """Attach conservative cohort evidence to every job from one company."""
    decisions = [explicit_job_cohort(job) for job in jobs]
    campaign = campaign or unknown_cohort()
    has_project_cohort = any(
        years_in_text(str(job.get("campaign_text") or ""))
        for job in jobs
    )
    has_unknown = any(
        decision["cohort_status"] == "unknown"
        for decision in decisions
    )
    if has_unknown and inspect_page:
        campaign = inspect_official_campaign(careers_url)

    checked_at = datetime.now().isoformat(timespec="seconds")
    for job, decision in zip(jobs, decisions):
        final = decision
        if campaign.get("campaign_scope") == "trusted_source_override":
            final = campaign
        elif (
            decision["cohort_status"] == "unknown"
            and campaign["cohort_status"] == "confirmed"
            and campaign.get("campaign_scope") in {
                "exclusive",
                "formal_with_internships",
                "trusted_source_fallback",
                "trusted_source_override",
            }
            and not has_project_cohort
        ):
            final = campaign
        job.update(final)
        job["cohort_checked_at"] = checked_at
        if not is_confirmed_current(job) and not str(job.get("jd_raw") or "").strip():
            job["jd_status"] = "not_required"
    return jobs


def annotate_crawled_jobs(
    companies: list[dict],
    jobs: list[dict],
    workers: int = 10,
    evidence_path: str | Path | None = None,
    refresh_campaigns: bool = False,
) -> list[dict]:
    """Classify all crawled companies, inspecting unresolved official entries."""
    config_by_name = {item["name"]: item for item in companies}
    grouped: dict[str, list[dict]] = {
        str(item.get("name") or ""): []
        for item in companies
        if str(item.get("name") or "")
    }
    for job in jobs:
        grouped.setdefault(str(job.get("company") or ""), []).append(job)
    output = Path(evidence_path) if evidence_path else (
        Path(__file__).resolve().parent / "outputs" / "company_campaign_evidence.json"
    )
    cached_evidence = {}
    if output.exists() and not refresh_campaigns:
        try:
            cached_evidence = {
                str(item.get("company") or ""): item
                for item in json.loads(output.read_text(encoding="utf-8"))
            }
        except (OSError, ValueError, TypeError):
            cached_evidence = {}
    max_age_hours = max(
        1,
        int(os.environ.get("CAMPAIGN_EVIDENCE_MAX_AGE_HOURS", "36")),
    )

    def cached_campaign(name: str, url: str) -> dict | None:
        item = cached_evidence.get(name)
        if not item or str(item.get("campaign_url") or "") != url:
            return None
        if (
            item.get("campaign_scope") == "trusted_source_fallback"
            and trusted_source_campaign(
                config_by_name.get(name) or {},
                jobs_observed=bool(rows),
            ) is None
        ):
            return None
        try:
            checked = datetime.fromisoformat(str(item.get("checked_at") or ""))
            age_hours = (datetime.now() - checked).total_seconds() / 3600
        except (TypeError, ValueError):
            return None
        return item if age_hours <= max_age_hours else None

    def classify(name: str, rows: list[dict]) -> tuple[list[dict], dict]:
        config = config_by_name.get(name) or {}
        campaign_url = str(
            config.get("campaign_url") or config.get("careers_url") or ""
        )
        has_unknown = not rows or any(
            explicit_job_cohort(job)["cohort_status"] == "unknown"
            for job in rows
        )
        campaign = cached_campaign(name, campaign_url) if has_unknown else None
        if campaign is None:
            campaign = (
                inspect_official_campaign(campaign_url)
                if has_unknown
                else unknown_cohort(
                    source="岗位字段已全部明确届别，无需继承公司活动证据",
                    campaign_url=campaign_url,
                )
            )
        if campaign["cohort_status"] == "unknown":
            campaign = trusted_source_campaign(
                config,
                jobs_observed=bool(rows),
            ) or campaign
        return (
            annotate_company_jobs(
                rows,
                campaign_url,
                inspect_page=False,
                campaign=campaign,
            ),
            campaign,
        )

    classified = []
    campaign_evidence = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(grouped) or 1))) as pool:
        futures = {
            pool.submit(classify, name, rows): name
            for name, rows in grouped.items()
        }
        for future in as_completed(futures):
            rows, campaign = future.result()
            name = futures[future]
            classified.extend(rows)
            campaign_evidence[name] = {
                **campaign,
                "company": name,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            [campaign_evidence[name] for name in sorted(campaign_evidence)],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return classified
