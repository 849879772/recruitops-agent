from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Mapping

from apps.api.codex_bff import CodexBffService
from packages.automation import AutomationRunResult, AutomationStore, ClaimedAutomation
from packages.codex_runtime.events import CodexEventType


class CodexAutomationExecutor:
    """Run one persistent automation through the same Codex harness as chat."""

    def __init__(
        self,
        service: CodexBffService,
        store: AutomationStore,
        *,
        timeout_seconds: float = 600.0,
        reconnect_grace_seconds: float = 90.0,
        settings: Any | None = None,
        task_handlers: Mapping[str, Any] | None = None,
    ) -> None:
        self.service = service
        self.store = store
        self.timeout_seconds = max(10.0, timeout_seconds)
        self.reconnect_grace_seconds = max(0.01, reconnect_grace_seconds)
        # Injected dependencies exist so tests can run the same dispatch path
        # against an isolated store.  When both are omitted the executor keeps
        # its production behaviour of resolving the global configuration.
        self._settings = settings
        self._task_handlers = task_handlers

    def _runtime_handlers(self, settings: Any) -> Mapping[str, Any]:
        if self._task_handlers is not None:
            return self._task_handlers
        from packages.scheduler.runtime import build_runtime_task_handlers

        return build_runtime_task_handlers(settings=settings)

    async def __call__(self, task: ClaimedAutomation) -> AutomationRunResult:
        if task.task_id == "daily_recruitment_intelligence":
            from packages.config import get_settings
            from packages.scheduler.models import TaskContext
            from packages.automation.latest_report import report_path, summarize, write_json_atomic

            settings = self._settings or get_settings()
            if not settings.write_enabled:
                return AutomationRunResult(status="blocked", error="write_disabled")
            if task.target_kind not in {"all", "company", "source"} or (
                task.target_kind in {"company", "source"} and not task.target_id
            ):
                return AutomationRunResult(status="blocked", error="invalid_sync_scope")
            context = TaskContext(task_id=task.task_id, task_label=task.task_label,
                scheduled_for=task.scheduled_for, run_id=task.execution_id, attempt=1,
                read_only=False, write_enabled=settings.write_enabled,
                metadata={"details": {"mode": "full", "company_ids": [task.target_id]
                    if task.target_kind == "company" and task.target_id else [],
                    "source_record_ids": [task.target_id]
                    if task.target_kind == "source" and task.target_id else []}})
            # Crawl/score stages retain their own bounded requests and checkpoints.
            # A chat-turn time limit must not decide the outcome of this pipeline.
            try:
                handler = self._runtime_handlers(settings)[task.task_id]
                result = await asyncio.to_thread(handler, context)
            except Exception as exc:
                # Return a redacted failure; rethrowing lets the worker persist
                # the original provider exception, including its credentials.
                result = {
                    "status": "failed",
                    "error": f"流水线执行异常：{type(exc).__name__}: {exc}",
                }
            report = summarize(result, task.execution_id, settings=settings)
            write_json_atomic(report_path(settings), report)
            return AutomationRunResult(
                status="succeeded" if result.get("status") == "completed" else "failed",
                summary="全量任务已结束；部分公司或评分未完成" if report["status"] == "partial" else
                    ("全量任务已完成" if report["status"] == "succeeded" else f"全量任务执行失败：{report['error']}"),
                error=report.get("error"),
            )
        if task.task_id == "crawler_health":
            from packages.config import get_settings
            from packages.scheduler.models import TaskContext

            settings = self._settings or get_settings()
            context = TaskContext(task_id=task.task_id, task_label=task.task_label,
                                  scheduled_for=task.scheduled_for, run_id=task.execution_id, attempt=1)
            handler = self._runtime_handlers(settings)[task.task_id]
            result = await asyncio.to_thread(handler, context)
            report_path = Path(settings.agent_root) / ".data" / "scheduler" / f"{task.execution_id}.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
            summary = {key: result.get(key) for key in ("status", "company_total", "integration_status_counts")}
            summary["report_path"] = str(report_path)
            return AutomationRunResult(status="succeeded" if result.get("status") == "observed" else "failed",
                                       summary=json.dumps(summary, ensure_ascii=False))
        thread = await self.service.thread_start()
        subscription = self.service.subscribe(thread.id)
        chunks: list[str] = []
        turn_id: str | None = None
        try:
            turn = await self.service.turn_start(thread.id, self._prompt(task))
            turn_id = turn.id
            await asyncio.to_thread(
                self.store.mark_running_context,
                task.execution_id,
                thread_id=thread.id,
                turn_id=turn.id,
            )

            async def wait_for_completion() -> AutomationRunResult:
                reconnect_deadline: float | None = None
                last_reconnect_error: str | None = None
                while True:
                    try:
                        if reconnect_deadline is None:
                            event = await subscription.get()
                        else:
                            remaining = reconnect_deadline - asyncio.get_running_loop().time()
                            if remaining <= 0:
                                return await self._network_failure(
                                    thread_id=thread.id,
                                    turn_id=turn.id,
                                    chunks=chunks,
                                    detail=last_reconnect_error,
                                )
                            event = await asyncio.wait_for(subscription.get(), timeout=remaining)
                    except TimeoutError:
                        return await self._network_failure(
                            thread_id=thread.id,
                            turn_id=turn.id,
                            chunks=chunks,
                            detail=last_reconnect_error,
                        )
                    except StopAsyncIteration:
                        error = (
                            self._network_error(last_reconnect_error)
                            if last_reconnect_error
                            else "Codex event stream closed before the turn completed"
                        )
                        return AutomationRunResult(
                            status="failed",
                            summary="".join(chunks).strip() or None,
                            error=error,
                            thread_id=thread.id,
                            turn_id=turn.id,
                        )

                    if event.turn_id and event.turn_id != turn.id:
                        continue
                    if event.event_type is CodexEventType.TEXT_DELTA and event.text:
                        chunks.append(event.text)
                    if event.event_type is CodexEventType.ERROR:
                        if self._is_reconnect_error(event.text):
                            last_reconnect_error = event.text
                            reconnect_deadline = reconnect_deadline or (
                                asyncio.get_running_loop().time()
                                + self.reconnect_grace_seconds
                            )
                            continue
                        return AutomationRunResult(
                            status="failed",
                            summary="".join(chunks).strip() or None,
                            error=event.text or "Codex automation turn failed",
                            thread_id=thread.id,
                            turn_id=turn.id,
                        )
                    if event.event_type is CodexEventType.TURN_COMPLETED:
                        summary = "".join(chunks).strip()
                        return AutomationRunResult(
                            status=self._terminal_status(summary),
                            summary=summary[-8_000:] or "Agent turn completed.",
                            thread_id=thread.id,
                            turn_id=turn.id,
                        )
                    if event.turn_id == turn.id and event.event_type in {
                        CodexEventType.TEXT_DELTA,
                        CodexEventType.ITEM_STARTED,
                        CodexEventType.ITEM_COMPLETED,
                    }:
                        reconnect_deadline = None
                        last_reconnect_error = None

            return await asyncio.wait_for(wait_for_completion(), timeout=self.timeout_seconds)
        except TimeoutError:
            if turn_id is not None:
                await self.service.turn_interrupt(thread.id, turn_id)
            return AutomationRunResult(
                status="failed",
                summary="".join(chunks).strip()[-8_000:] or None,
                error=f"automation exceeded {self.timeout_seconds:.0f}s timeout",
                thread_id=thread.id,
                turn_id=turn_id,
            )
        finally:
            subscription.close()

    async def _network_failure(
        self,
        *,
        thread_id: str,
        turn_id: str,
        chunks: list[str],
        detail: str | None,
    ) -> AutomationRunResult:
        await self.service.turn_interrupt(thread_id, turn_id)
        return AutomationRunResult(
            status="failed",
            summary="".join(chunks).strip()[-8_000:] or None,
            error=self._network_error(detail),
            thread_id=thread_id,
            turn_id=turn_id,
        )

    @staticmethod
    def _network_error(detail: str | None) -> str:
        suffix = f" Last runtime message: {detail}" if detail else ""
        return f"CODEX_NETWORK_UNAVAILABLE: reconnect grace period expired.{suffix}"

    @staticmethod
    def _is_reconnect_error(message: str | None) -> bool:
        normalized = (message or "").casefold()
        return any(
            token in normalized
            for token in (
                "reconnecting",
                "waiting for network",
                "connection reset",
                "connection temporarily unavailable",
            )
        )

    @staticmethod
    def _terminal_status(summary: str) -> str:
        normalized = summary.casefold()
        normalized = re.sub(r"(?:需要登录(?:或验证)?|验证码|证据不足|无法确认|执行失败)\s*[:：]?\s*`?0\b`?", "", normalized)
        if not normalized.strip() or any(token in normalized for token in (
            "无法按该名称启动", "任务未完成", "运行尚无最终结果", "工具不可用", "task failed",
        )):
            return "failed"
        blocked_tokens = (
            "state_unclear",
            "captcha_required",
            "需要登录",
            "验证码",
            "证据不足",
            "无法确认",
            "edge 未连接",
            "edge未连接",
        )
        return "blocked" if any(token in normalized for token in blocked_tokens) else "succeeded"

    @staticmethod
    def _prompt(task: ClaimedAutomation) -> str:
        if task.task_id == "recruitment_mailbox":
            return ("本地定时邮箱任务：调用 recruitment_mail_process，timeout_ms=90000，"
                    "自动同步后处理待处理邮件；若 has_more=true 则继续下一批，直到 scope_complete=true。"
                    "不要创建计划，不要等待自己的 automation_execution 完成。中文简短报告处理数、剩余数和失败数。")
        if task.task_id == "application_progress":
            scope = (
                f"只复核 application_id={task.target_id}（{task.target_label or '指定投递'}）"
                if task.target_id
                else "复核当前所有未终态投递记录"
            )
            selection = (
                "按指定 application_id 调用 batch_observe_application_status。"
                if task.target_id else
                "直接调用 batch_observe_application_status(all_non_terminal=true)，"
                "不要先查询全部记录。若 remaining_count>0，只传返回的 run_id 继续调用，"
                "直到 scope_complete=true；按 scope_total 报告范围，不得再减 excluded_terminal，"
                "累计计数不相加。"
            )
            return (
                "这是本地持久化计划触发的投递进度复核，不是用户咨询。"
                f"{scope}。{selection}"
                "由工具完成页面观测和状态校验；include_vision=false；不要读取本地技能文件或调用未开放的连接工具。"
                "本定时任务禁止调用视觉分析，"
                "即使 DOM 证据不足也只归类为无法确认或需要登录或验证，不做第二次视觉观察；"
                "通过统一状态校验写入有明确证据的状态变化，包括淘汰；不得用旧投递确认覆盖终态。"
                "不得询问是否继续，不得创建新的计划，不得改为爬虫或云端任务。"
                "最后简洁说明目标、页面证据、数据库是否变化；若阻塞，明确错误码和可操作原因。"
            )
        scope = ""
        if task.task_id == "daily_recruitment_intelligence" and task.target_id:
            field = "source_record_ids" if task.target_kind == "source" else "company_ids"
            scope = f"限定参数 {field}={json.dumps([task.target_id], ensure_ascii=False)}，不得扩大范围。"
        return (
            "这是本地持久化计划触发的招聘情报任务。立即调用 daily_recruitment_sync，mode=full，"
            f"{scope}"
            "不得创建新的计划或询问是否继续。最后报告结构化结果。"
            "如果返回运行中，用 daily_recruitment_sync_status 等待该 run_id 的最终结果，"
            "timeout_ms=120000；不要轮询 automation_schedule_list 等待自身结束。启动成功不等于任务完成。"
        )


__all__ = ["CodexAutomationExecutor"]
