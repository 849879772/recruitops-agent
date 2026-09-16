from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.metrics import WebsiteEvalResult, summarize_websites
from evals.website_blind import load_website_blind_fixture


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score observed crawler results against the frozen 30-site manifest."
    )
    parser.add_argument("observed", type=Path)
    parser.add_argument("--fixture", type=Path, default=None)
    args = parser.parse_args()

    fixture = load_website_blind_fixture(args.fixture)
    observed_payload = json.loads(args.observed.read_text(encoding="utf-8"))
    observed = {
        str(item["company"]).casefold(): item
        for item in observed_payload
    }
    results = []
    for case in fixture.cases:
        item = observed.get(case.company.casefold(), {})
        results.append(
            WebsiteEvalResult(
                company=case.company,
                connected=bool(item.get("connected", False)),
                expected_jobs=case.expected_jobs,
                found_jobs=int(item.get("found_jobs", 0)),
                correct_titles=int(item.get("correct_titles", 0)),
                complete_jd=int(item.get("complete_jd", 0)),
                erroneous_writes=int(item.get("erroneous_writes", 0)),
                manual_interventions=int(item.get("manual_interventions", 0)),
                steps=int(item.get("steps", 0)),
                token_cost=float(item.get("token_cost", 0.0)),
            )
        )

    summary = summarize_websites(results)
    print(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
