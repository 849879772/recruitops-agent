#!/usr/bin/env python
"""Batch observe and safely reconcile all applied applications.

The extension gathers structured evidence. The local verifier applies only
high-confidence forward updates and writes an audit row for each decision.

Usage:
    python scripts/batch_check_application_status.py
    python scripts/batch_check_application_status.py --application-ids 25 24 22
    python scripts/batch_check_application_status.py --timeout 60000 --max-concurrent 3
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import Settings
from packages.browser_bridge import BrowserBridgeStore
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import Storage
from packages.tools.batch_browser_operations import (
    BatchObserveApplicationStatusInput,
    batch_observe_application_status,
)


async def main():
    parser = argparse.ArgumentParser(
        description="Batch check application status for multiple applications"
    )
    parser.add_argument(
        "--application-ids",
        nargs="+",
        help="Specific application IDs to check (default: all 'applied' status)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45000,
        help="Timeout per unique status page in milliseconds (default: 45000)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="Maximum concurrent operations (default: 3)",
    )
    parser.add_argument(
        "--no-skip-captcha",
        action="store_true",
        help="Do not skip applications that require CAPTCHA",
    )
    parser.add_argument(
        "--no-skip-login",
        action="store_true",
        help="Do not skip applications that require login",
    )

    args = parser.parse_args()

    # Initialize dependencies
    settings = Settings()
    storage = Storage.from_url(settings.database_url)
    repository = PostgresRecruitmentRepository(storage)
    browser_bridge = BrowserBridgeStore(storage)

    # Get application IDs
    if args.application_ids:
        application_ids = args.application_ids
        print(f"Checking {len(application_ids)} specified applications...")
    else:
        # Get all 'applied' status applications
        all_apps = repository.list_applications()
        applied_apps = [app for app in all_apps if app.stage == "applied"]
        application_ids = [str(app.id) for app in applied_apps]
        print(f"Found {len(application_ids)} applications with 'applied' status")

    if not application_ids:
        print("No applications to check.")
        return

    # Create request
    request = BatchObserveApplicationStatusInput(
        application_ids=application_ids,
        timeout_per_application_ms=args.timeout,
        max_concurrent=args.max_concurrent,
        skip_on_captcha=not args.no_skip_captcha,
        skip_on_login=not args.no_skip_login,
    )

    print(f"\nConfiguration:")
    print(f"  Timeout per page: {args.timeout}ms")
    print(f"  Max concurrent: {args.max_concurrent}")
    print("  Vision fallback: disabled (use an individual assistant follow-up when justified)")
    print(f"  Skip CAPTCHA: {not args.no_skip_captcha}")
    print(f"  Skip login required: {not args.no_skip_login}")
    print(f"\nStarting batch check...\n")

    # Execute batch check
    response = await batch_observe_application_status(
        request,
        browser_bridge,
        repository,
    )

    # Print results
    print("=" * 80)
    print(f"Batch Check Results")
    print("=" * 80)
    print(f"\nTotal: {response.total}")
    print(f"Unique pages: {response.pages_total}")
    print(f"Updated: {len(response.updated)}")
    print(f"Unchanged: {len(response.unchanged)}")
    print(f"Blocked: {len(response.blocked)}")
    print(f"Unresolved: {len(response.unresolved)}")
    print(f"Failed: {len(response.failed)}")
    print(f"Database writes: {response.summary['write_count']}")
    print(f"Elapsed: {response.elapsed_ms}ms ({response.elapsed_ms / 1000:.1f}s)")

    if response.updated:
        print(f"\nUpdated ({len(response.updated)}):")
        print("-" * 80)
        for result in response.updated:
            print(
                f"  Application {result.application_id}: {result.observed_status} "
                f"(written={result.wrote})"
            )

    if response.unchanged:
        print(f"\nUnchanged ({len(response.unchanged)}):")
        print("-" * 80)
        for result in response.unchanged:
            print(f"  Application {result.application_id}: {result.observed_status}")

    if response.blocked:
        print(f"\nBlocked ({len(response.blocked)}):")
        print("-" * 80)
        for result in response.blocked:
            print(f"  Application {result.application_id}: {result.reason}")

    if response.unresolved:
        print(f"\nUnresolved ({len(response.unresolved)}):")
        print("-" * 80)
        for result in response.unresolved:
            print(f"  Application {result.application_id}: {result.reason}")

    if response.failed:
        print(f"\nFailed ({len(response.failed)}):")
        print("-" * 80)
        for result in response.failed:
            print(f"  Application {result.application_id}: {result.reason}")
            print(f"    Elapsed: {result.elapsed_ms}ms")

    print("\n" + "=" * 80)

    print("\n" + "=" * 80)
    print(f"Batch check completed with tool status: {response.status.value}")

    if response.blocked:
        print(f"\nNote: {len(response.blocked)} applications need login or CAPTCHA handling.")

    if response.failed:
        print(f"\nNote: {len(response.failed)} applications failed.")
        print("Check technical failure reasons above and retry only those pages.")


if __name__ == "__main__":
    asyncio.run(main())
