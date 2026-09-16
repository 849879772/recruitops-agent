from packages.discovery import (
    CompanyReconciliationResult,
    SourceLead,
    SourceSyncResult,
)


def test_discovery_results_are_typed_and_serializable() -> None:
    lead = SourceLead(
        canonical_name=" 示例科技 ",
        source="oc_snapshot",
        source_urls=("https://example.test/jobs", "https://example.test/jobs"),
        metadata={"industry": "软件"},
    )
    sync = SourceSyncResult(
        source="oc_snapshot",
        source_url="https://www.givemeoc.com/",
        leads=(lead,),
        rows_seen=2,
        accepted_rows=1,
        pages_fetched=1,
    )
    reconciliation = CompanyReconciliationResult(existing=(lead,))

    assert lead.company_name == "示例科技"
    assert lead.source_url == "https://example.test/jobs"
    assert sync.companies == (lead,)
    assert reconciliation.counts == {"existing": 1, "new": 0, "ambiguous": 0}
    assert reconciliation.to_dict()["existing"][0]["canonical_name"] == "示例科技"

