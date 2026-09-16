from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import Settings  # noqa: E402
from packages.matching import DeepSeekClient, MatchingService  # noqa: E402
from packages.matching.resume import (  # noqa: E402
    build_analysis_resume_plan,
    resume_pending_analyses,
)
from packages.storage import Storage  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resume title-screened, detail-complete jobs without a successful score."
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan without model calls")
    parser.add_argument("--limit", type=int, default=None, help="maximum pending jobs to process")
    parser.add_argument("--concurrency", type=int, default=None, help="bounded model concurrency")
    return parser


def main() -> int:
    args = _parser().parse_args()
    settings = Settings()
    profile_payload = yaml.safe_load(
        Path(settings.candidate_profile_config).read_text(encoding="utf-8")
    ) or {}
    profile = profile_payload.get("profile", profile_payload)
    storage = Storage.from_url(settings.database_url)
    plan = build_analysis_resume_plan(storage, profile, limit=args.limit)
    print(json.dumps({"plan": plan.as_dict()}, ensure_ascii=False))
    if args.dry_run or not plan.pending_jobs:
        return 0
    if not settings.write_enabled:
        raise SystemExit("RECRUITOPS_WRITE_ENABLED must be true for analysis writes")
    if not settings.llm_enabled:
        raise SystemExit("RECRUITOPS_LLM_ENABLED must be true for model analysis")
    client = DeepSeekClient(
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        endpoint=settings.llm_endpoint,
        timeout=settings.llm_timeout_seconds,
        max_tokens=settings.llm_matching_max_tokens,
        thinking_enabled=settings.llm_matching_thinking_enabled,
        reasoning_effort=settings.llm_matching_reasoning_effort,
    )
    service = MatchingService(client, max_tokens=settings.llm_matching_max_tokens)

    def report_progress(result):
        print(json.dumps({"progress": result.as_dict()}, ensure_ascii=False), flush=True)

    result = resume_pending_analyses(
        storage,
        profile,
        service,
        plan.pending_jobs,
        concurrency=args.concurrency or settings.match_max_concurrency,
        progress=report_progress,
    )
    print(json.dumps({"result": result.as_dict()}, ensure_ascii=False))
    return 2 if result.stopped_reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
