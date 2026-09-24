"""One observable control plane for the complete daily recruitment run.

The harness chooses when to invoke this service. The service itself remains a
deterministic coordinator: it calls bounded domain stages in a fixed order,
records every transition, and never asks a model to fan out over companies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import StrEnum
import inspect
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.models import TaskRun, TaskStatus
from packages.pipeline.daily import PipelineInterrupted


class DailySyncStage(StrEnum):
    DISCOVERY = "discovery"
    RECONCILIATION = "reconciliation"
    CRAWL = "crawl"
    OFFLINE_RECONCILIATION = "offline_reconciliation"
    REPORTING = "reporting"


class StageStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    PAUSED = "paused"


class DailySyncStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    PAUSED = "paused"


class StageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    stage: DailySyncStage
    status: StageStatus
    observed_at: datetime
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class DailySyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: DailySyncStatus
    dry_run: bool
    started_at: datetime
    finished_at: datetime
    stages: list[StageEvent]
    discovery: Any = None
    reconciliation: Any = None
    pipeline: Any = None
    offline_reconciliation: Any = None
    report: Any = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class StateStore(Protocol):
    def save_task_run(self, task_run: TaskRun) -> None: ...


Clock = Callable[[], datetime]
EventSink = Callable[[StageEvent], None]
DiscoveryStage = Callable[..., Any]
ReconciliationStage = Callable[[Any], Any]
CrawlStage = Callable[[bool], Any]
OfflineStage = Callable[[Any, bool], Any]
ReportingStage = Callable[[Any, Any, Any], Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        return _json_safe(converter())
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _summary_metrics(value: Any) -> dict[str, Any]:
    payload = _json_safe(value)
    if not isinstance(payload, Mapping):
        return {}
    preferred = (
        "total",
        "source_count",
        "lead_count",
        "existing_count",
        "new_count",
        "ambiguous_count",
        "selected_companies",
        "crawled_companies",
        "changed_count",
        "reused_count",
        "failed_company_count",
        "inactive_count",
        "restored_count",
        "issue_count",
    )
    return {
        key: payload[key]
        for key in preferred
        if key in payload and isinstance(payload[key], (str, int, float, bool))
    }


class DailyRecruitmentSync:
    """Coordinate one daily run while preserving deterministic stage ownership."""

    def __init__(
        self,
        *,
        crawl: CrawlStage,
        discovery: DiscoveryStage | None = None,
        reconcile: ReconciliationStage | None = None,
        offline_reconcile: OfflineStage | None = None,
        report: ReportingStage | None = None,
        state_store: StateStore | None = None,
        event_sink: EventSink | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self.discovery = discovery
        self.reconcile = reconcile
        self.crawl = crawl
        self.offline_reconcile = offline_reconcile
        self.report = report
        self.state_store = state_store
        self.event_sink = event_sink
        self.clock = clock

    def run(self, *, run_id: str | None = None, dry_run: bool = False) -> DailySyncResult:
        actual_run_id = run_id or uuid4().hex
        started_at = self.clock()
        events: list[StageEvent] = []
        warnings: list[str] = []
        outputs: dict[DailySyncStage, Any] = {}
        fatal_error: str | None = None
        paused = False
        pause_reason: str | None = None

        self._persist_task(
            actual_run_id,
            status=TaskStatus.RUNNING,
            current_step="starting",
            step_count=0,
            started_at=started_at,
        )

        def emit(
            stage: DailySyncStage,
            status: StageStatus,
            summary: str,
            *,
            value: Any = None,
            error: str | None = None,
        ) -> StageEvent:
            event = StageEvent(
                sequence=len(events) + 1,
                stage=stage,
                status=status,
                observed_at=self.clock(),
                summary=summary,
                metrics=_summary_metrics(value),
                error=error,
            )
            events.append(event)
            if self.event_sink is not None:
                self.event_sink(event)
            self._persist_task(
                actual_run_id,
                status=TaskStatus.RUNNING,
                current_step=f"{stage.value}:{status.value}",
                step_count=len(events),
                started_at=started_at,
            )
            return event

        discovery_output = None
        if self.discovery is None:
            emit(DailySyncStage.DISCOVERY, StageStatus.SKIPPED, "未配置外部来源发现器")
        else:
            emit(DailySyncStage.DISCOVERY, StageStatus.RUNNING, "正在同步可信招聘来源")
            try:
                parameters = inspect.signature(self.discovery).parameters
                discovery_output = (
                    self.discovery(dry_run) if parameters else self.discovery()
                )
                outputs[DailySyncStage.DISCOVERY] = discovery_output
                emit(
                    DailySyncStage.DISCOVERY,
                    StageStatus.SUCCEEDED,
                    "可信招聘来源同步完成",
                    value=discovery_output,
                )
            except Exception as exc:
                message = f"来源发现失败，继续抓取已配置公司：{type(exc).__name__}: {exc}"
                warnings.append(message)
                emit(
                    DailySyncStage.DISCOVERY,
                    StageStatus.FAILED,
                    "可信招聘来源同步失败",
                    error=message,
                )

        reconciliation_output = None
        if self.reconcile is None or discovery_output is None:
            emit(
                DailySyncStage.RECONCILIATION,
                StageStatus.SKIPPED,
                "没有可用于公司对账的来源结果",
            )
        else:
            emit(DailySyncStage.RECONCILIATION, StageStatus.RUNNING, "正在对账公司与入口")
            try:
                reconciliation_output = self.reconcile(discovery_output)
                outputs[DailySyncStage.RECONCILIATION] = reconciliation_output
                emit(
                    DailySyncStage.RECONCILIATION,
                    StageStatus.SUCCEEDED,
                    "公司与入口对账完成",
                    value=reconciliation_output,
                )
            except Exception as exc:
                message = f"公司对账失败，继续抓取已配置公司：{type(exc).__name__}: {exc}"
                warnings.append(message)
                emit(
                    DailySyncStage.RECONCILIATION,
                    StageStatus.FAILED,
                    "公司与入口对账失败",
                    error=message,
                )

        pipeline_output = None
        emit(DailySyncStage.CRAWL, StageStatus.RUNNING, "正在执行固定公司批量抓取与增量分析")
        try:
            pipeline_output = self.crawl(dry_run)
            outputs[DailySyncStage.CRAWL] = pipeline_output
            emit(
                DailySyncStage.CRAWL,
                StageStatus.SUCCEEDED,
                "批量抓取与增量分析完成",
                value=pipeline_output,
            )
        except PipelineInterrupted as exc:
            paused = True
            pause_reason = exc.reason_code
            emit(
                DailySyncStage.CRAWL,
                StageStatus.PAUSED,
                "已安全暂停；可按原任务范围继续",
                error=str(exc),
            )
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}"
            emit(
                DailySyncStage.CRAWL,
                StageStatus.FAILED,
                "批量抓取失败，停止后续写入阶段",
                error=fatal_error,
            )

        offline_output = None
        if fatal_error is not None or paused or self.offline_reconcile is None:
            emit(
                DailySyncStage.OFFLINE_RECONCILIATION,
                StageStatus.SKIPPED,
                "抓取未完成或未配置岗位离线收口",
            )
        else:
            emit(
                DailySyncStage.OFFLINE_RECONCILIATION,
                StageStatus.RUNNING,
                "正在复核本轮缺失岗位与宽限下线",
            )
            try:
                offline_output = self.offline_reconcile(pipeline_output, dry_run)
                outputs[DailySyncStage.OFFLINE_RECONCILIATION] = offline_output
                emit(
                    DailySyncStage.OFFLINE_RECONCILIATION,
                    StageStatus.SUCCEEDED,
                    "岗位离线收口完成",
                    value=offline_output,
                )
            except Exception as exc:
                message = f"岗位离线收口失败：{type(exc).__name__}: {exc}"
                warnings.append(message)
                emit(
                    DailySyncStage.OFFLINE_RECONCILIATION,
                    StageStatus.FAILED,
                    "岗位离线收口失败，未据此删除岗位",
                    error=message,
                )

        report_output = None
        if fatal_error is not None or paused or self.report is None:
            emit(
                DailySyncStage.REPORTING,
                StageStatus.SKIPPED,
                "抓取失败或未配置运行报告器",
            )
        else:
            emit(DailySyncStage.REPORTING, StageStatus.RUNNING, "正在生成运行报告")
            try:
                report_output = self.report(
                    pipeline_output,
                    reconciliation_output,
                    offline_output,
                )
                outputs[DailySyncStage.REPORTING] = report_output
                emit(
                    DailySyncStage.REPORTING,
                    StageStatus.SUCCEEDED,
                    "运行报告生成完成",
                    value=report_output,
                )
            except Exception as exc:
                message = f"运行报告生成失败：{type(exc).__name__}: {exc}"
                warnings.append(message)
                emit(
                    DailySyncStage.REPORTING,
                    StageStatus.FAILED,
                    "运行报告生成失败",
                    error=message,
                )

        finished_at = self.clock()
        status = (
            DailySyncStatus.FAILED
            if fatal_error is not None
            else DailySyncStatus.PAUSED
            if paused
            else DailySyncStatus.DEGRADED
            if warnings
            else DailySyncStatus.SUCCEEDED
        )
        self._persist_task(
            actual_run_id,
            status=TaskStatus.FAILED if fatal_error else TaskStatus.STOPPED if paused else TaskStatus.SUCCEEDED,
            current_step="paused" if paused else "completed" if fatal_error is None else "failed",
            step_count=len(events),
            started_at=started_at,
            error_code="crawl_failed" if fatal_error else pause_reason if paused else None,
        )
        return DailySyncResult(
            run_id=actual_run_id,
            status=status,
            dry_run=dry_run,
            started_at=started_at,
            finished_at=finished_at,
            stages=events,
            discovery=_json_safe(discovery_output),
            reconciliation=_json_safe(reconciliation_output),
            pipeline=_json_safe(pipeline_output),
            offline_reconciliation=_json_safe(offline_output),
            report=_json_safe(report_output),
            warnings=warnings,
            error=fatal_error or pause_reason,
        )

    def _persist_task(
        self,
        run_id: str,
        *,
        status: TaskStatus,
        current_step: str,
        step_count: int,
        started_at: datetime,
        error_code: str | None = None,
    ) -> None:
        if self.state_store is None:
            return
        now = self.clock()
        self.state_store.save_task_run(
            TaskRun(
                id=run_id,
                task_type="daily_recruitment_sync",
                status=status,
                user_request="本地每日招聘情报统一同步",
                current_step=current_step,
                step_count=step_count,
                max_steps=10,
                error_code=error_code,
                created_at=started_at,
                updated_at=now,
                source="recruitops-agent.codex-harness",
                source_ref=f"daily-sync:{run_id}",
            )
        )


__all__ = [
    "DailyRecruitmentSync",
    "DailySyncResult",
    "DailySyncStage",
    "DailySyncStatus",
    "StageEvent",
    "StageStatus",
]
