from packages.domain.job_identity import build_job_identity, normalize_job_identity_url


def test_identity_ignores_location_and_tracking_parameters() -> None:
    company = {"id": "unit-a", "organization_id": "org-a"}
    first = build_job_identity(
        company,
        {
            "id": "native-1", "title": "软件开发工程师", "city": "杭州",
            "detail_url": "https://Jobs.Example.com/job/1?utm_source=oc&jobId=1",
        },
    )
    second = build_job_identity(
        company,
        {
            "id": "changed-native-id", "title": "软件开发工程师", "city": "北京",
            "detail_url": "https://jobs.example.com/job/1?jobId=1&from=share",
        },
    )
    assert first.business_key == second.business_key
    assert first.stable_id == second.stable_id
    assert first.native_job_id == "native-1"


def test_same_title_in_different_units_is_distinct_without_detail_url() -> None:
    job = {"title": "软件开发工程师", "department": "平台研发"}
    left = build_job_identity(
        {"id": "unit-left", "organization_id": "org"}, job
    )
    right = build_job_identity(
        {"id": "unit-right", "organization_id": "org"}, job
    )
    assert left.business_key != right.business_key


def test_same_organization_title_and_detail_url_merges_across_units() -> None:
    job = {"title": "算法工程师", "detail_url": "https://ats.example/jobs/42"}
    left = build_job_identity({"id": "unit-a", "organization_id": "org"}, job)
    right = build_job_identity({"id": "unit-b", "organization_id": "org"}, job)
    assert left.business_key == right.business_key


def test_url_keeps_job_parameters_and_spa_route_but_drops_tracking() -> None:
    assert normalize_job_identity_url(
        "https://EXAMPLE.com/jobs?jobId=7&utm_source=x#/detail/7?from=share&project=2"
    ) == "https://example.com/jobs?jobid=7#/detail/7?project=2"
