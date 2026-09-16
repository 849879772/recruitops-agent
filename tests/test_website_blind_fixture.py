from evals.website_blind import load_website_blind_fixture


def test_website_blind_fixture_contains_thirty_unique_frozen_companies() -> None:
    fixture = load_website_blind_fixture()

    assert len(fixture.cases) == 30
    assert len({case.company.casefold() for case in fixture.cases}) == 30
    assert all(case.expected_jobs > 0 for case in fixture.cases)
    assert all(case.expected_status == "HEALTHY" for case in fixture.cases)
