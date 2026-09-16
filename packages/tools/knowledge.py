from __future__ import annotations

from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.rag import Citation, EvidenceGrounder

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolResponse, ToolStatus


class KnowledgeSearchInput(ToolInput):
    query: str = Field(default="", max_length=4_000)
    domain: Literal["crawler", "candidate", "personal"] = "crawler"
    top_k: int = Field(default=5, ge=1, le=20)
    action: Literal["search", "list", "read"] = "search"
    document_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    page: int = Field(default=1, ge=1, le=80)
    offset: int = Field(default=0, ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "search" and not self.query.strip():
            raise ValueError("search requires a query")
        if self.action != "search" and self.domain != "personal":
            raise ValueError("list/read are available for personal knowledge only")
        if self.action == "read" and not self.document_id:
            raise ValueError("read requires document_id")
        return self


class KnowledgeSearchData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str
    domain: Literal["crawler", "candidate", "personal"]
    answerable: bool
    context: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    refusal_reason: str | None = None
    documents: list[dict] = Field(default_factory=list)
    next_offset: int | None = None


def search_personal_knowledge(request, service):
    started = perf_counter()
    evidence = [EvidenceSource(source="personal_knowledge", source_ref=request.document_id or "personal")]
    try:
        documents, hits, next_offset = [], [], None
        if request.action == "list":
            documents = service.list_documents()
        elif request.action == "read":
            doc = service.read(request.document_id, page=request.page, revision=request.revision)
            body = doc.pop("text")
            content = body[request.offset:request.offset + 6000]
            if not content:
                raise ValueError("读取位置超出正文范围")
            if request.offset + 6000 < len(body):
                next_offset = request.offset + 6000
            hits = [{**doc, "document_id": doc["id"], "chunk_id": doc["id"], "content": content,
                     "url": f"/?knowledge={doc['id']}&page={request.page}&revision={doc['revision']}"}]
        else:
            hits = service.search(request.query, document_id=request.document_id, top_k=min(6, request.top_k))
        citations = [Citation(chunk_id=hit["chunk_id"], source="personal_knowledge", source_ref=hit["url"],
                              snippet=hit["content"], metadata={key: value for key, value in hit.items() if key != "content"}) for hit in hits]
        ok = bool(hits) or request.action == "list"
        data = KnowledgeSearchData(query=request.query, domain="personal", answerable=bool(hits),
                                   context=[hit["content"] for hit in hits], citations=citations,
                                   documents=documents, next_offset=next_offset,
                                   refusal_reason=None if ok else "未找到可引用资料；不代表用户没有相应能力。")
        elapsed = int((perf_counter() - started) * 1000)
        if elapsed > request.timeout_ms:
            return KnowledgeSearchResponse(tool_name="knowledge_search", status=ToolStatus.FAILURE, success=False,
                                           evidence=evidence, error_code=ToolErrorCode.TIMEOUT,
                                           error_message="知识检索超过时间预算，请缩小资料范围后重试",
                                           read_only=True, timeout_ms=request.timeout_ms, elapsed_ms=elapsed, timed_out=True)
        return KnowledgeSearchResponse(tool_name="knowledge_search", status=ToolStatus.SUCCESS if ok else ToolStatus.NO_RESULTS,
                                       success=ok, data=data, evidence=evidence, read_only=True, timeout_ms=request.timeout_ms,
                                       error_code=None if ok else ToolErrorCode.NO_RESULTS,
                                       error_message=None if ok else data.refusal_reason, elapsed_ms=elapsed)
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ValueError, LookupError)) else "个人知识库暂不可用，请检查资料索引和向量服务"
        return KnowledgeSearchResponse(tool_name="knowledge_search", status=ToolStatus.FAILURE, success=False,
                                       evidence=evidence,
                                       error_code=ToolErrorCode.SOURCE_UNAVAILABLE, error_message=message,
                                       read_only=True, timeout_ms=request.timeout_ms,
                                       elapsed_ms=int((perf_counter()-started)*1000))


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
