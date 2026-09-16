from __future__ import annotations

from datetime import date, datetime, time, timezone
from time import perf_counter

from pydantic import Field, model_validator

from packages.domain.models import ScheduleEvent
from packages.repositories.base import RecruitmentRepository

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolModel, ToolResponse, ToolStatus


class ScheduleWindowInput(ToolInput):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_window(self) -> "ScheduleWindowInput":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if (self.end_date - self.start_date).days > 90:
            raise ValueError("schedule window cannot exceed 90 days")
        return self


class ScheduleConflict(ToolModel):
    first_event_id: str
    second_event_id: str
    starts_at: datetime
    ends_at: datetime


class ScheduleWindowData(ToolModel):
    events: list[ScheduleEvent] = Field(default_factory=list)
    conflicts: list[ScheduleConflict] = Field(default_factory=list)
    unscheduled_event_ids: list[str] = Field(default_factory=list)


class ScheduleWindowResponse(ToolResponse[ScheduleWindowData]):
    pass


def _bounds(event: ScheduleEvent) -> tuple[datetime, datetime] | None:
    if event.status != "pending" or event.time_kind != "appointment" or event.ends_at is None:
        return None
    start = event.starts_at
    if start is None and event.event_date is not None and event.event_time is not None:
        start = datetime.combine(event.event_date, event.event_time)
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = event.ends_at
    if end.tzinfo is None:
        end = end.replace(tzinfo=start.tzinfo)
    if end <= start:
        return None
    return start, end


def inspect_schedule_window(
    request: ScheduleWindowInput,
    repository: RecruitmentRepository,
) -> ScheduleWindowResponse:
    started = perf_counter()
    events = []
    for item in repository.list_schedule():
        event = ScheduleEvent.model_validate(item)
        if event.event_date is None or request.start_date <= event.event_date <= request.end_date:
            events.append(event)
    events.sort(key=lambda item: (item.event_date or date.max, item.event_time or time.max, item.id))
    timed = [(event, _bounds(event)) for event in events]
    conflicts: list[ScheduleConflict] = []
    for index, (first, first_bounds) in enumerate(timed):
        if first_bounds is None:
            continue
        for second, second_bounds in timed[index + 1 :]:
            if second_bounds is None:
                continue
            overlap_start = max(first_bounds[0], second_bounds[0])
            overlap_end = min(first_bounds[1], second_bounds[1])
            if overlap_start < overlap_end:
                conflicts.append(
                    ScheduleConflict(
                        first_event_id=first.id,
                        second_event_id=second.id,
                        starts_at=overlap_start,
                        ends_at=overlap_end,
                    )
                )
    data = ScheduleWindowData(
        events=events,
        conflicts=conflicts,
        unscheduled_event_ids=[event.id for event, bounds in timed if bounds is None],
    )
    return ScheduleWindowResponse(
        tool_name="schedule_window",
        status=ToolStatus.SUCCESS if events else ToolStatus.NO_RESULTS,
        success=bool(events),
        data=data,
        evidence=[EvidenceSource(source="source_schedule", source_ref="schedule.json")],
        error_code=None if events else ToolErrorCode.NO_RESULTS,
        error_message=None if events else "No schedule events exist in the requested window.",
        timeout_ms=request.timeout_ms,
        elapsed_ms=int((perf_counter() - started) * 1000),
    )


__all__ = [
    "ScheduleConflict",
    "ScheduleWindowData",
    "ScheduleWindowInput",
    "ScheduleWindowResponse",
    "inspect_schedule_window",
]
