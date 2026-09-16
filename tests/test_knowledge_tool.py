from packages.rag import DocumentChunk, EvidenceGrounder, LexicalCosineRetriever
from packages.tools.knowledge import KnowledgeSearchInput, search_knowledge
from packages.tools.typed import ToolStatus


def _grounder() -> EvidenceGrounder:
    retriever = LexicalCosineRetriever()
    retriever.add(
        [
            DocumentChunk(
                id="moka-1",
                content="Moka 校招岗位需要遍历全部分页并读取详情接口。",
                source="crawler_knowledge",
                source_ref="docs://moka",
                metadata={"domain": "crawler"},
            ),
            DocumentChunk(
                id="resume-1",
                content="候选人完成过 ROS 机械臂开发。",
                source="candidate_profile",
                source_ref="profile://local",
                metadata={"domain": "candidate"},
            ),
        ]
    )
    return EvidenceGrounder(retriever, minimum_score=0.15)


def test_knowledge_search_filters_domain_and_returns_citations() -> None:
    response = search_knowledge(
        KnowledgeSearchInput(query="Moka 分页", domain="crawler"),
        _grounder(),
    )

    assert response.status is ToolStatus.SUCCESS
    assert response.data is not None
    assert response.data.citations[0].source_ref == "docs://moka"
    assert all(item.source_ref != "profile://local" for item in response.evidence)


def test_knowledge_search_refuses_without_evidence() -> None:
    response = search_knowledge(
        KnowledgeSearchInput(query="Kubernetes operator", domain="crawler"),
        _grounder(),
    )

    assert response.status is ToolStatus.NO_RESULTS
    assert response.data is not None and response.data.answerable is False
