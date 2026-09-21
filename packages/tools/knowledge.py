from __future__ import annotations

from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.rag import Citation, EvidenceGrounder

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolResponse, ToolStatus


class KnowledgeSearchInput(ToolInput):
    query: str = Field(default="", max_length=4_000)
    domain: Literal["crawler", "candidate"] = "crawler"
    top_k: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_query(self):
        if not self.query.strip():
            raise ValueError("search requires a query")
        return self


class KnowledgeSearchData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str
    domain: Literal["crawler", "candidate"]
    answerable: bool
    context: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    refusal_reason: str | None = None


class KnowledgeSearchResponse(ToolResponse[KnowledgeSearchData]):
    pass


def search_knowledge(
    request: KnowledgeSearchInput,
    grounder: EvidenceGrounder,
) -> KnowledgeSearchResponse:
    started = perf_counter()
    try:
        grounded = grounder.collect(
            request.query,
            top_k=request.top_k,
            metadata_filter={"domain": request.domain},
        )
    except Exception:
        return KnowledgeSearchResponse(
            tool_name="knowledge_search",
            status=ToolStatus.FAILURE,
            success=False,
            evidence=[EvidenceSource(source="rag_store", source_ref=request.domain)],
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
            error_message="The approved knowledge store was unavailable.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=max(0, int((perf_counter() - started) * 1_000)),
            read_only=True,
        )
    elapsed = max(0, int((perf_counter() - started) * 1_000))
    citations = list(grounded.citations)
    evidence = [EvidenceSource(source="rag_store", source_ref=request.domain)]
    evidence.extend(
        EvidenceSource(source=item.source, source_ref=item.source_ref)
        for item in citations
    )
    data = KnowledgeSearchData(
        query=grounded.query,
        domain=request.domain,
        answerable=grounded.answerable,
        context=grounded.context,
        citations=citations,
        refusal_reason=grounded.refusal_reason,
    )
    if elapsed > request.timeout_ms:
        return KnowledgeSearchResponse(
            tool_name="knowledge_search",
            status=ToolStatus.FAILURE,
            success=False,
            data=data,
            evidence=evidence,
            error_code=ToolErrorCode.TIMEOUT,
            error_message="The knowledge search exceeded its timeout budget.",
            timeout_ms=request.timeout_ms,
            timed_out=True,
            elapsed_ms=elapsed,
            read_only=True,
        )
    if not grounded.answerable:
        return KnowledgeSearchResponse(
            tool_name="knowledge_search",
            status=ToolStatus.NO_RESULTS,
            success=False,
            data=data,
            evidence=evidence,
            error_code=ToolErrorCode.NO_RESULTS,
            error_message="No approved knowledge evidence exceeded the retrieval threshold.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=elapsed,
            read_only=True,
        )
    return KnowledgeSearchResponse(
        tool_name="knowledge_search",
        status=ToolStatus.SUCCESS,
        success=True,
        data=data,
        evidence=evidence,
        timeout_ms=request.timeout_ms,
        elapsed_ms=elapsed,
        read_only=True,
    )


__all__ = [
    "KnowledgeSearchData",
    "KnowledgeSearchInput",
    "KnowledgeSearchResponse",
    "search_knowledge",
]
