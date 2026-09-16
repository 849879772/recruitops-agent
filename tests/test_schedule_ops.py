from datetime import date, datetime, time, timezone

from packages.domain.models import ApplicationStage, ScheduleEvent
from packages.tools import ScheduleWindowInput, ToolStatus, inspect_schedule_window


class Repository:
    def list_schedule(self, on_date=None):
        return [
            ScheduleEvent(
                id="event-1", title="A interview", event_date=date(2026, 8, 21),
                event_time=time(10, 0), event_type="面试", company_name="A",
                job_title="Engineer", application_stage=ApplicationStage.INTERVIEW1,
                starts_at=datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc),
                ends_at=datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc),
                source="test", source_ref="1",
            ),
            ScheduleEvent(
                id="event-2", title="B written", event_date=date(2026, 8, 21),
                event_time=time(10, 30), event_type="笔试", company_name="B",
                job_title="Developer", application_stage=ApplicationStage.WRITTEN,
                starts_at=datetime(2026, 8, 21, 10, 30, tzinfo=timezone.utc),
                ends_at=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
                source="test", source_ref="2",
            ),
            ScheduleEvent(
                id="event-3", title="Deadline", event_date=date(2026, 8, 22),
                event_type="截止", company_name="C", job_title="C++",
                application_stage=ApplicationStage.APPLIED, source="test", source_ref="3",
            ),
        ]


def test_schedule_window_reports_real_overlap_and_unknown_time() -> None:
    result = inspect_schedule_window(
        ScheduleWindowInput(start_date=date(2026, 8, 21), end_date=date(2026, 8, 22)),
        Repository(),
    )

    assert result.status is ToolStatus.SUCCESS
    assert [item.id for item in result.data.events] == ["event-1", "event-2", "event-3"]
    assert len(result.data.conflicts) == 1
    assert result.data.conflicts[0].starts_at.hour == 10
    assert result.data.conflicts[0].starts_at.minute == 30
    assert result.data.unscheduled_event_ids == ["event-3"]


def test_empty_window_is_no_results() -> None:
    result = inspect_schedule_window(
        ScheduleWindowInput(start_date=date(2026, 9, 1), end_date=date(2026, 9, 2)),
        Repository(),
    )
    assert result.status is ToolStatus.NO_RESULTS
    assert result.data.events == []


def test_pending_date_and_deadline_do_not_create_fake_conflicts():
    repo = Repository()
    events = repo.list_schedule()
    events[0].time_kind = "deadline"
    events[1].ends_at = None
    events[2].event_date = None
    repo.list_schedule = lambda: events
    result = inspect_schedule_window(
        ScheduleWindowInput(start_date=date(2026,8,21),end_date=date(2026,8,22)),repo)
    assert len(result.data.events) == 3
    assert result.data.conflicts == []
    assert set(result.data.unscheduled_event_ids) == {"event-1","event-2","event-3"}
