"""Local same-origin API for the shared schedule-item snapshot table."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, NoReturn
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from apps.api.local_ui import _storage
from packages.domain.models import ScheduleEvent
from packages.tools.schedule_manage import (
    ScheduleConflictError,
    ScheduleEventCreateFields,
    ScheduleEventPatchFields,
    ScheduleManageData,
    ScheduleManager,
    ScheduleNotFoundError,
    ScheduleValidationError,
)


router = APIRouter(prefix="/api/local-ui", tags=["local-ui"])


class ScheduleEventPatchRequest(ScheduleEventPatchFields):
    expected_updated_at: datetime


class ScheduleEventMutationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["created", "updated"]
    event: ScheduleEvent


def _map_error(error: Exception) -> NoReturn:
    if isinstance(error, ScheduleNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ScheduleConflictError):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, ScheduleValidationError):
        raise HTTPException(status_code=422, detail=str(error)) from error
    raise error


@router.post(
    "/events",
    response_model=ScheduleEventMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_schedule_event(body: ScheduleEventCreateFields) -> ScheduleEventMutationResponse:
    try:
        data = ScheduleManager(_storage()).create(
            body,
            source="local_ui",
            source_ref="local-ui:" + uuid4().hex,
        )
    except (ScheduleNotFoundError, ScheduleConflictError, ScheduleValidationError) as error:
        _map_error(error)
    return ScheduleEventMutationResponse(status="created", event=data.event)


@router.patch(
    "/events/{event_id}",
    response_model=ScheduleEventMutationResponse,
)
def update_schedule_event(
    event_id: str,
    body: ScheduleEventPatchRequest,
) -> ScheduleEventMutationResponse:
    try:
        data = ScheduleManager(_storage()).update(
            event_id,
            body,
            expected_updated_at=body.expected_updated_at,
        )
    except (ScheduleNotFoundError, ScheduleConflictError, ScheduleValidationError) as error:
        _map_error(error)
    return ScheduleEventMutationResponse(status="updated", event=data.event)


__all__ = [
    "ScheduleEventMutationResponse",
    "ScheduleEventPatchRequest",
    "create_schedule_event",
    "router",
    "update_schedule_event",
]
