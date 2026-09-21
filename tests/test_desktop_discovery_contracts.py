from packages.config import Settings
from packages.discovery.public_entries import PublicSearchHit, discover_company_entry_candidates
from packages.storage import Storage
from packages.tools.public_entry_discovery import PublicEntryDiscoveryInput, discover_public_recruitment_entries
from scripts import run_mcp_server


def test_cohort_queries_ranking_and_tool_forwarding_are_consistent():
    class Provider:
        def search(self, query, timeout):
            return [PublicSearchHit(provider="fixture", query=query,
                        url=f"https://careers.example.test/campus/{year}",
                        title=f"Synthetic {year} campus recruitment", snippet="Official recruitment")
                    for year in (2027, 2028)]

    queries, candidates = discover_company_entry_candidates("Synthetic", provider=Provider(), cohort_year=2028)
    assert "2028" in queries[1] and "2027" not in queries[1]
    assert candidates[0].hit.url.endswith("2028")
    response = discover_public_recruitment_entries(
        PublicEntryDiscoveryInput(company_names=["Synthetic"], cohort_year=2028), provider=Provider())
    assert response.success
    assert response.data.companies[0].queries == queries
    assert response.data.companies[0].candidates[0].url.endswith("2028")


def test_mcp_uses_saved_settings_scope_without_profile_scope(tmp_path, monkeypatch):
    settings = Settings(agent_root=tmp_path, database_url=f"sqlite:///{tmp_path / 'fixture.db'}",
                        offerbiu_industry_groups=["finance", "internet-tech"])
    Storage.from_url(settings.database_url, initialize=True)
    monkeypatch.setattr(run_mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(run_mcp_server, "_evidence_grounder", lambda *args: None)
    monkeypatch.setattr(run_mcp_server, "create_fastmcp_server", lambda *args, **kwargs: kwargs)
    result = run_mcp_server.build_server()
    assert result["offerbiu_refresher"].scope == {"industry_groups": ["finance", "internet-tech"]}
