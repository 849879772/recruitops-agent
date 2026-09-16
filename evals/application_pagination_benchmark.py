from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from fastapi.testclient import TestClient

from apps.api import main as api
from apps.api.main import app
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import ApplicationSnapshot, Storage


def _seed(storage: Storage, count: int) -> None:
    batch_size = 500
    with storage.transaction() as session:
        for start in range(0, count, batch_size):
            session.add_all(
                ApplicationSnapshot(
                    id=f"synthetic-app-{index:06d}",
                    company_name=f"隔离测试公司 {index % 250:03d}",
                    job_title=f"隔离测试岗位 {index:06d}",
                    job_id=f"synthetic-job-{index:06d}",
                    record_url=f"https://example.invalid/applications/{index}",
                    stage="applied",
                    idempotency_key=f"synthetic-application:{index}",
                    stage_history=[],
                    source="isolated-pagination-benchmark",
                    source_ref=f"synthetic:{index}",
                )
                for index in range(start, min(start + batch_size, count))
            )


def _timed(call, repeats: int) -> tuple[list[float], object]:
    elapsed: list[float] = []
    result = None
    for _ in range(repeats):
        started = perf_counter()
        result = call()
        elapsed.append((perf_counter() - started) * 1000)
    return elapsed, result


def run_benchmark(*, records: int = 5_000, page_size: int = 50, repeats: int = 5) -> dict[str, object]:
    if records < 1 or page_size < 1 or repeats < 1:
        raise ValueError("records, page_size and repeats must be positive")
    with TemporaryDirectory(prefix="recruitops-applications-") as directory:
        database = Path(directory) / "isolated.db"
        storage = Storage.from_url(f"sqlite+pysqlite:///{database.as_posix()}", initialize=True)
        _seed(storage, records)
        repository = PostgresRecruitmentRepository(storage)
        offsets = (0, max(0, records // 2), max(0, records - page_size))

        repository_metrics: dict[str, object] = {}
        for offset in offsets:
            timings, page = _timed(
                lambda offset=offset: repository.search_applications(limit=page_size, offset=offset),
                repeats,
            )
            assert page is not None
            assert page.total == records
            assert len(page.items) <= page_size
            repository_metrics[str(offset)] = {
                "median_ms": round(median(timings), 3),
                "max_ms": round(max(timings), 3),
                "items": len(page.items),
            }

        api.app.dependency_overrides[api.repository] = lambda: repository
        client = TestClient(app)
        try:
            timings, response = _timed(
                lambda: client.get(
                    "/api/applications/page",
                    params={"limit": page_size, "offset": records // 2},
                ),
                repeats,
            )
        finally:
            client.close()
            api.app.dependency_overrides.clear()
        assert response is not None
        response.raise_for_status()
        payload = response.json()
        assert payload["total"] == records
        assert len(payload["items"]) <= page_size

        report = {
            "isolated": True,
            "database": "temporary SQLite deleted after benchmark",
            "formal_database_writes": 0,
            "records": records,
            "page_size": page_size,
            "repeats": repeats,
            "repository_pages": repository_metrics,
            "api_middle_page": {
                "median_ms": round(median(timings), 3),
                "max_ms": round(max(timings), 3),
                "items": len(payload["items"]),
                "response_bytes": len(response.content),
            },
        }
        storage.engine.dispose()
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark isolated application pagination.")
    parser.add_argument("--records", type=int, default=5_000)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_benchmark(records=args.records, page_size=args.page_size, repeats=args.repeats)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
