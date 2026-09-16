from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from .models import Citation
from .retriever import Retriever


class GroundedEvidence(BaseModel):
    """Citation-bearing context that downstream decision nodes may trust."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1)
    answerable: bool
    context: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    refusal_reason: str | None = None


class EvidenceGrounder:
    """Turn retrieval results into an auditable context or an explicit refusal."""

    def __init__(self, retriever: Retriever, *, minimum_score: float = 0.2) -> None:
        if not 0.0 <= minimum_score <= 1.0:
            raise ValueError("minimum_score must be between 0 and 1")
        self.retriever = retriever
        self.minimum_score = minimum_score

    def collect(
        self,
        query: str,
        *,
        top_k: int = 5,
        metadata_filter: Mapping[str, object] | None = None,
    ) -> GroundedEvidence:
        normalized = query.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        results = self.retriever.search(
            normalized,
            top_k=top_k,
            metadata_filter=metadata_filter,
        )
        accepted = [result for result in results if result.score >= self.minimum_score]
        if not accepted:
            return GroundedEvidence(
                query=normalized,
                answerable=False,
                refusal_reason="no_retrieval_evidence_above_threshold",
            )
        return GroundedEvidence(
            query=normalized,
            answerable=True,
            context=[result.chunk.content for result in accepted],
            citations=[
                result.citation.model_copy(
                    update={
                        "metadata": {
                            **result.citation.metadata,
                            "retrieval_score": round(result.score, 6),
                            "lexical_score": round(result.lexical_score, 6),
                            "cosine_score": round(result.cosine_score, 6),
                        }
                    }
                )
                for result in accepted
            ],
        )


__all__ = ["EvidenceGrounder", "GroundedEvidence"]
