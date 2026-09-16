from evals.application_pagination_benchmark import run_benchmark


def test_application_pagination_benchmark_is_isolated_and_bounded() -> None:
    report = run_benchmark(records=250, page_size=25, repeats=1)

    assert report["isolated"] is True
    assert report["formal_database_writes"] == 0
    assert report["records"] == 250
    assert report["api_middle_page"]["items"] == 25
    assert all(page["items"] <= 25 for page in report["repository_pages"].values())
