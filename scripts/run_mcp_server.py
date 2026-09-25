"""Run RecruitOps Agent's MCP server over stdio."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import Settings, get_settings  # noqa: E402
from packages.automation import AutomationStore  # noqa: E402
from packages.browser_bridge import BrowserBridgeStore  # noqa: E402
from packages.rag import (  # noqa: E402
    DeterministicEmbeddingProvider,
    EvidenceGrounder,
    PgVectorDocumentStore,
)
from packages.mcp import (  # noqa: E402
    MCP_AGENT_TOOL_NAMES,
    MCP_READ_ONLY_TOOL_NAMES,
    MCP_TOOL_NAMES,
    MCP_TOOL_PROTOCOL_VERSION,
    create_fastmcp_server,
)
from packages.recruitment_mail import RecruitmentMailStore  # noqa: E402
from packages.repositories.base import RecruitmentRepository  # noqa: E402
from packages.repositories.postgres import PostgresRecruitmentRepository  # noqa: E402
from packages.scheduler import (  # noqa: E402
    DEFAULT_LOCK_PATH,
    LocalTaskScheduler,
    build_runtime_task_handlers,
)
from packages.storage import AgentStateStore, Storage  # noqa: E402
from packages.discovery.company_registry import CompanySourceRegistry  # noqa: E402
from packages.discovery.offerbiu_refresh import OfferBiuRefreshService  # noqa: E402
from packages.tools.crawler_run import (  # noqa: E402
    ConfiguredCrawlerRunner,
    RecruitmentCoreCrawlerProcess,
)
from packages.tools.operations import OperationalTaskRunner  # noqa: E402
from packages.tools.oc_candidates import OcCandidateRunner  # noqa: E402


def _evidence_grounder(configured: Settings, storage: Storage) -> EvidenceGrounder:
    engine = storage.engine
    # Remote compatible embedding providers are retired. This is a separate
    # local index; never rewrite old semantic vectors using another embedder.
    store = PgVectorDocumentStore(engine, DeterministicEmbeddingProvider())
    store.ensure_schema()
    return EvidenceGrounder(store)


def build_server(
    *,
    settings: Settings | None = None,
    repository: RecruitmentRepository | None = None,
    mail_store: RecruitmentMailStore | None = None,
    browser_bridge_store: BrowserBridgeStore | None = None,
    profile: str = "agent",
):
    configured = settings or get_settings()
    storage = Storage.from_url(configured.database_url)
    repo = repository or PostgresRecruitmentRepository(storage)
    store = mail_store or RecruitmentMailStore(
        storage
    )
    bridge_store = browser_bridge_store or BrowserBridgeStore(
        storage
    )
    scheduler = LocalTaskScheduler(
        lock_path=configured.agent_root / DEFAULT_LOCK_PATH
    )
    crawler_runner = ConfiguredCrawlerRunner(
        configured.companies_config,
        RecruitmentCoreCrawlerProcess(configured.companies_config),
    )
    oc_candidate_runner = OcCandidateRunner(
        configured.agent_root / ".data/discovery/retired-source.json",
        configured.companies_config,
    )
    operation_runner = OperationalTaskRunner(
        scheduler,
        build_runtime_task_handlers(
            settings=configured,
            browser_bridge_store=bridge_store,
        ),
        state_store=AgentStateStore(storage),
    )
    return create_fastmcp_server(
        repo,
        store,
        bridge_store,
        evidence_grounder=_evidence_grounder(configured, storage),
        crawler_runner=crawler_runner,
        oc_candidate_runner=oc_candidate_runner,
        operational_task_runner=operation_runner,
        offerbiu_refresher=OfferBiuRefreshService(
            CompanySourceRegistry(storage),
            scope={"industry_groups": configured.offerbiu_industry_groups},
        ),
        automation_scheduler=scheduler,
        automation_store=AutomationStore(storage),
        profile=profile,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print the frozen protocol and tool surface without database access.",
    )
    parser.add_argument(
        "--profile",
        choices=("agent", "full", "read_only"),
        default="agent",
        help="Select the model-visible MCP surface (default: agent).",
    )
    args = parser.parse_args()
    if args.check:
        print(
            json.dumps(
                {
                    "protocol_version": MCP_TOOL_PROTOCOL_VERSION,
                    "profile": args.profile,
                    "tools": list(
                        MCP_AGENT_TOOL_NAMES
                        if args.profile == "agent"
                        else MCP_TOOL_NAMES
                        if args.profile == "full"
                        else MCP_READ_ONLY_TOOL_NAMES
                    ),
                    "full_tool_count": len(MCP_TOOL_NAMES),
                },
                ensure_ascii=False,
            )
        )
        return
    server = build_server(profile=args.profile)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
