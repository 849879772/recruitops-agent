"""Run the frozen no-key reliability evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.reliability import (
    DEFAULT_FIXTURE_PATH,
    render_reliability_report,
    run_reliability_eval,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the offline no-key reliability evaluation.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    report = run_reliability_eval(fixture_path=args.fixture)
    serialized = render_reliability_report(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if report.accuracy == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
