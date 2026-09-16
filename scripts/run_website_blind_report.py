"""Report the frozen 30-company blind evaluation in offline or live mode."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import sys
import time
from collections.abc import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.website_blind import (
    WebsiteBlindCase,
    WebsiteBlindObservation,
    evaluate_website_blind,
    load_website_blind_fixture,
)


_NON_JOB_TITLE_RE = re.compile(
    r"^(?:职位|岗位|招聘|校园招聘|社会招聘|招聘职位|职位列表|全部|更多|"
    r"技术类|研发类|产品类|市场类|职能类|综合类|查看详情|立即申请)$",
    re.I,
)


def _valid_job_title(value: object) -> bool:
    title = " ".join(str(value or "").split())
    return 2 <= len(title) <= 120 and _NON_JOB_TITLE_RE.fullmatch(title) is None


def _run_live_case(
    case: WebsiteBlindCase,
    *,
    timeout_seconds: float = 180.0,
    hydrate_details: bool = False,
    detail_timeout_seconds: float = 120.0,
    detail_workers: int = 4,
) -> dict[str, object]:
    from packages.pipeline.isolation import (
        IsolatedOperationTimeout,
        IsolatedWorkerError,
        crawl_company_isolated,
        fetch_job_detail_isolated,
    )
    from packages.matching import screen_job
    from packages.recruitment_core.job_details import is_jd_incomplete

    started = time.perf_counter()
    company = {
        "name": case.company,
        "crawler": case.crawler,
        "careers_url": str(case.source_url),
    }
    failure_categories: list[str] = []
    error = ""
    try:
        jobs = list(crawl_company_isolated(company, timeout_seconds=timeout_seconds))
    except IsolatedOperationTimeout as exc:
        jobs = []
        error = str(exc)
        failure_categories.append("crawler_timeout")
    except IsolatedWorkerError as exc:
        jobs = []
        error = str(exc)
        failure_categories.append(
            f"crawler_{str(exc.error_type or 'worker_error').casefold()}"
        )
    except Exception as exc:  # noqa: BLE001 - blind run must isolate every site
        jobs = []
        error = f"{type(exc).__name__}: {exc}"
        failure_categories.append("crawler_exception")

    found_jobs = len(jobs)
    normalized_jobs = [job for job in jobs if isinstance(job, Mapping)]
    production_candidate_indexes: list[int] = []
    for index, job in enumerate(normalized_jobs):
        screening = screen_job(dict(job))
        status = getattr(screening.analysis_status, "value", screening.analysis_status)
        if screening.eligible or (
            str(status) == "jd_incomplete" and screening.matched_directions
        ):
            production_candidate_indexes.append(index)

    hydration_attempted = 0
    hydration_succeeded = 0
    if hydrate_details:
        pending_indexes = [
            index
            for index in production_candidate_indexes
            if is_jd_incomplete(dict(normalized_jobs[index]))
        ]
        hydration_attempted = len(pending_indexes)
        if pending_indexes:
            with ThreadPoolExecutor(
                max_workers=min(max(1, detail_workers), len(pending_indexes))
            ) as executor:
                futures = {
                    executor.submit(
                        fetch_job_detail_isolated,
                        dict(normalized_jobs[index]),
                        timeout_seconds=detail_timeout_seconds,
                    ): index
                    for index in pending_indexes
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        detail = str(future.result() or "").strip()
                    except Exception:  # noqa: BLE001 - one detail must not stop a blind case
                        detail = ""
                    hydrated = {**dict(normalized_jobs[index]), "jd_raw": detail}
                    if detail and not is_jd_incomplete(hydrated):
                        normalized_jobs[index] = hydrated
                        hydration_succeeded += 1

    complete_jd = sum(not is_jd_incomplete(dict(job)) for job in normalized_jobs)
    production_complete_jd = sum(
        not is_jd_incomplete(dict(normalized_jobs[index]))
        for index in production_candidate_indexes
    )
    correct_titles = sum(_valid_job_title(job.get("title")) for job in normalized_jobs)
    if not jobs and not failure_categories:
        failure_categories.append("crawler_empty")
    if (
        hydrate_details
        and production_complete_jd < len(production_candidate_indexes)
    ):
        failure_categories.append("production_jd_incomplete")
    return {
        "case_id": case.case_id,
        "company": case.company,
        "connected": bool(jobs),
        "found_jobs": found_jobs,
        "found_detail_jobs": complete_jd,
        "correct_titles": correct_titles,
        "complete_jd": complete_jd,
        "production_eligible_jobs": len(production_candidate_indexes),
        "production_eligible_complete_jd": production_complete_jd,
        "hydration_attempted": hydration_attempted,
        "hydration_succeeded": hydration_succeeded,
        "steps": 1 + hydration_attempted,
        "manual_interventions": int(bool(failure_categories)),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "failure_categories": failure_categories,
        "error": error,
        "sample_titles": [str(job.get("title") or "") for job in normalized_jobs[:5]],
    }


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_observations(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("observations", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("observation checkpoint must contain a list")
    return [dict(item) for item in values if isinstance(item, Mapping)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score the frozen 30-company manifest without changing project state."
    )
    parser.add_argument("observed", type=Path, nargs="?")
    parser.add_argument("--fixture", type=Path, default=None)
    parser.add_argument("--live", action="store_true", help="Explicitly run current crawlers.")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--observations-output", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--hydrate-details",
        action="store_true",
        help="Hydrate only confirmed 2027 target-direction jobs, matching production.",
    )
    parser.add_argument("--detail-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--detail-workers", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    fixture = load_website_blind_fixture(args.fixture)

    if args.live:
        checkpoint = args.observations_output or Path(
            ".data/evals/website_blind_live_observations.json"
        )
        observations = _load_observations(checkpoint) if args.resume else []
        completed = {str(item.get("case_id") or "") for item in observations}
        for index, case in enumerate(fixture.cases, start=1):
            if case.case_id in completed:
                print(
                    f"[{index}/{len(fixture.cases)}] skip {case.case_id} {case.company}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            print(
                f"[{index}/{len(fixture.cases)}] run {case.case_id} {case.company}",
                file=sys.stderr,
                flush=True,
            )
            observation = _run_live_case(
                case,
                timeout_seconds=args.timeout_seconds,
                hydrate_details=args.hydrate_details,
                detail_timeout_seconds=args.detail_timeout_seconds,
                detail_workers=args.detail_workers,
            )
            observations.append(observation)
            _atomic_write_json(
                checkpoint,
                {"version": 1, "mode": "live", "observations": observations},
            )
            print(
                f"  jobs={observation['found_jobs']} complete_jd={observation['complete_jd']} "
                f"production_jd={observation['production_eligible_complete_jd']}/"
                f"{observation['production_eligible_jobs']} "
                f"failures={observation['failure_categories']} latency_ms={observation['latency_ms']}",
                file=sys.stderr,
                flush=True,
            )
        summary = evaluate_website_blind(
            fixture,
            (
                WebsiteBlindObservation.model_validate(
                    {key: value for key, value in item.items() if key not in {"error", "sample_titles"}}
                )
                for item in observations
            ),
            mode="live",
            synthetic=False,
        )
    else:
        if args.observed is None:
            parser.error("offline mode requires an observed JSON path")
        payload = json.loads(args.observed.read_text(encoding="utf-8"))
        observations = payload.get("observations", []) if isinstance(payload, dict) else payload
        if not isinstance(observations, list):
            parser.error("observed JSON must be a list or an observations object")
        summary = evaluate_website_blind(
            fixture,
            [WebsiteBlindObservation.model_validate(item) for item in observations],
            mode="offline",
            synthetic=True,
        )

    serialized = json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
