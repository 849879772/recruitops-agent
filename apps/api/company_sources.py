"""HTTP API for the durable company/source discovery registry."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.api.local_ui import is_local_ui, local_ui_request
from packages.config import get_settings
from packages.discovery.company_registry import (
    CompanySourceConflict,
    CompanySourceNotFound,
    CompanySourceRegistry,
    SOURCE_STATUSES,
)
from packages.discovery.company_source_retry import (
    CompanySourceRetryCapacity,
    CompanySourceRetryConflict,
    CompanySourceRetryRejected,
    CompanySourceRetryStartError,
)
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import Storage


router = APIRouter(prefix="/api/company-sources", tags=["company-sources"])
start_retry: Callable[[str], Any] | None = None


def set_start_retry_callback(callback: Callable[[str], Any] | None) -> None:
    """Bind the main-thread, bounded read-only crawler launcher."""

    global start_retry
    start_retry = callback


def _registry() -> CompanySourceRegistry:
    return CompanySourceRegistry(Storage.from_url(get_settings().database_url))


def _require_local_write(request: Request) -> None:
    if not local_ui_request.get() or not is_local_ui(request):
        raise HTTPException(status_code=403, detail="Local same-origin UI request required")
    if not get_settings().write_enabled:
        raise HTTPException(status_code=403, detail="Writes are disabled")


class EntryUrlPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_url: str = Field(min_length=1, max_length=2048)
    expected_updated_at: datetime


@router.get("")
def list_company_sources(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    q: str = Query(default="", max_length=255),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    if status is not None and status not in SOURCE_STATUSES - {"unusable"}:
        raise HTTPException(status_code=422, detail="Unsupported source status")
    return _registry().list_sources(page=page, page_size=page_size, q=q, status=status)


@router.get("/{record_id}")
def get_company_source(record_id: str) -> dict[str, Any]:
    record = _registry().get_source(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Company source not found")
    return record


@router.get("/{record_id}/jobs")
def list_company_source_jobs(
    record_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    result = PostgresRecruitmentRepository(_registry().storage).list_source_jobs(
        record_id, page=page, page_size=page_size
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Company source not found")
    return result


@router.patch("/{record_id}/entry")
def patch_company_source_entry(
    record_id: str, body: EntryUrlPatch, request: Request
) -> dict[str, Any]:
    _require_local_write(request)
    try:
        return _registry().set_entry_url(record_id, body.entry_url, body.expected_updated_at)
    except CompanySourceNotFound as exc:
        raise HTTPException(status_code=404, detail="Company source not found") from exc
    except CompanySourceConflict as exc:
        raise HTTPException(status_code=409, detail="Record changed; refresh and retry") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{record_id}/retry", status_code=202)
def retry_company_source(record_id: str, request: Request) -> dict[str, Any]:
    _require_local_write(request)
    callback = start_retry
    if callback is None:
        raise HTTPException(status_code=503, detail="Retry callback is not configured")
    registry = _registry()
    if registry.get_source(record_id) is None:
        raise HTTPException(status_code=404, detail="Company source not found")
    try:
        callback(record_id)
    except CompanySourceNotFound as exc:
        raise HTTPException(status_code=404, detail="Company source not found") from exc
    except (CompanySourceRetryConflict, CompanySourceRetryCapacity) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CompanySourceRetryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CompanySourceRetryStartError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Retry could not be started") from exc
    return {"id": record_id, "status": "running"}


__all__ = ["EntryUrlPatch", "router", "set_start_retry_callback", "start_retry"]
