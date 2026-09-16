from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class _RagModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class DocumentChunk(_RagModel):
    """A searchable, citation-preserving piece of a source document."""

    id: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("id", "chunk_id"),
    )
    content: str = Field(
        min_length=1,
        validation_alias=AliasChoices("content", "text"),
    )
    source: str = Field(min_length=1, max_length=128)
    source_ref: str = Field(min_length=1, max_length=2048)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def chunk_id(self) -> str:
        """Compatibility accessor for callers that use ``chunk_id`` terminology."""

        return self.id

    @property
    def text(self) -> str:
        """Compatibility accessor for text-oriented retrieval callers."""

        return self.content


class Citation(_RagModel):
    """The source information needed to substantiate one retrieval result."""

    chunk_id: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=128)
    source_ref: str = Field(min_length=1, max_length=2048)
    snippet: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(_RagModel):
    """A ranked chunk with component scores and a ready-to-use citation."""

    chunk: DocumentChunk
    score: float
    lexical_score: float
    cosine_score: float
    citation: Citation
