"""Typed MCP boundary for BIU source discovery and registration."""

from __future__ import annotations

from typing import Literal
from time import perf_counter

from pydantic import Field, model_validator

from packages.discovery.offerbiu_refresh import OfferBiuRefreshService

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolModel, ToolResponse, ToolStatus


class OfferBiuSourceRefreshInput(ToolInput):
    apply: bool = True
    max_pages: int = Field(default=150, ge=1, le=150)
    page_size: int = Field(default=50, ge=1, le=50)
    delay_seconds: float = Field(default=0.05, ge=0, le=2)

    @model_validator(mode="after")
    def require_complete_apply_budget(self) -> "OfferBiuSourceRefreshInput":
        if self.apply and self.max_pages < 30:
            raise ValueError("apply=true requires max_pages >= 30 for a complete source snapshot")
        return self


class OfferBiuPendingEntry(ToolModel):
    record_id: str
    company_name: str
    entry_url: str
    status: str
    job_count: int = Field(ge=0)


class OfferBiuSourceRefreshData(ToolModel):
    new_companies: int = Field(default=0, ge=0)
    excluded_reasons: dict[str, int] = Field(default_factory=dict)
    complete: bool
    stop_reason: str | None = None
    pages_fetched: int = Field(ge=0)
    records_seen: int = Field(ge=0)
    companies_seen: int = Field(ge=0)
    applied: bool
    registered_entries: int = Field(ge=0)
    new_entries: int = Field(ge=0)
    linked_existing_entries: int = Field(ge=0)
    excluded_unusable: int = Field(ge=0)
    out_of_scope: int = Field(ge=0)
    registered_ids: list[str] = Field(default_factory=list, max_length=20)
    pending_entries: list[OfferBiuPendingEntry] = Field(default_factory=list)


class OfferBiuSourceRefreshResponse(ToolResponse[OfferBiuSourceRefreshData]):
    read_only: Literal[False] = False


def refresh_offerbiu_sources(
    request: OfferBiuSourceRefreshInput,
    service: OfferBiuRefreshService,
) -> OfferBiuSourceRefreshResponse:
    started = perf_counter()
    payload = service.refresh(
        apply=request.apply,
        max_pages=request.max_pages,
        page_size=request.page_size,
        delay_seconds=request.delay_seconds,
    )
    elapsed_ms = max(0, int((perf_counter() - started) * 1_000))
    complete = bool(payload["complete"])
    return OfferBiuSourceRefreshResponse(
        tool_name="offerbiu_source_refresh",
        status=ToolStatus.SUCCESS if complete else ToolStatus.FAILURE,
        success=complete,
        data=OfferBiuSourceRefreshData.model_validate(payload),
        evidence=[EvidenceSource(
            source="offerbiu_public_api",
            source_ref="https://offerbiu.com/companies/",
        )],
        error_code=None if complete else ToolErrorCode.SOURCE_UNAVAILABLE,
        error_message=None if complete else f"BIU refresh stopped: {payload['stop_reason']}",
        timeout_ms=request.timeout_ms,
        elapsed_ms=elapsed_ms,
        read_only=False,
    )


__all__ = [
    "OfferBiuSourceRefreshData",
    "OfferBiuSourceRefreshInput",
    "OfferBiuSourceRefreshResponse",
    "refresh_offerbiu_sources",
]
