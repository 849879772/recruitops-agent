from packages.rag import (
    DocumentChunk,
    EvidenceGrounder,
    LexicalCosineRetriever,
)


def test_grounder_returns_context_with_source_citations() -> None:
    retriever = LexicalCosineRetriever(lexical_weight=1.0, cosine_weight=0.0)
    retriever.add(
        [
            DocumentChunk(
                id="candidate-1",
                content="机械臂项目使用 ROS2、C++ 和 MoveIt 完成运动规划。",
                source="candidate_evidence",
                source_ref="resume/projects/robot-arm",
                metadata={"owner": "user", "evidence_level": "verified"},
            )
        ]
    )

    evidence = EvidenceGrounder(retriever, minimum_score=0.1).collect(
        "ROS2 机械臂",
        metadata_filter={"owner": "user"},
    )

    assert evidence.answerable is True
    assert evidence.context == ["机械臂项目使用 ROS2、C++ 和 MoveIt 完成运动规划。"]
    assert evidence.citations[0].source_ref == "resume/projects/robot-arm"
    assert evidence.refusal_reason is None


def test_grounder_refuses_when_retrieval_has_no_supported_evidence() -> None:
    retriever = LexicalCosineRetriever(lexical_weight=1.0, cosine_weight=0.0)
    retriever.add(
        [
            DocumentChunk(
                id="crawler-1",
                content="Moka 平台通过接口总数完成分页。",
                source="crawler_knowledge",
                source_ref="docs/moka.md",
                metadata={"domain": "crawler"},
            )
        ]
    )

    evidence = EvidenceGrounder(retriever, minimum_score=0.5).collect(
        "候选人强化学习经历",
        metadata_filter={"domain": "candidate"},
    )

    assert evidence.answerable is False
    assert evidence.context == []
    assert evidence.citations == []
    assert evidence.refusal_reason == "no_retrieval_evidence_above_threshold"
