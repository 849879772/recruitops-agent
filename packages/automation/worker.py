from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .service import AutomationStore, ClaimedAutomation


@dataclass(frozen=True)
class AutomationRunResult:
    status: str
    summary: str | None = None
    error: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None


AutomationExecutor = Callable[[ClaimedAutomation], Awaitable[AutomationRunResult]]


class LocalAutomationWorker:
    """Poll persistent schedules and execute each claimed occurrence once."""

    def __init__(
        self,
        store: AutomationStore,
        executor: AutomationExecutor,
        *,
        poll_seconds: float = 10.0,
    ) -> None:
        self.store = store
        self.executor = executor
        self.poll_seconds = max(0.1, poll_seconds)
        self._stop = asyncio.Event()

    async def run_once(self) -> bool:
        claimed = await asyncio.to_thread(self.store.claim_due)
        if claimed is None:
            return False
        try:
            result = await self.executor(claimed)
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.complete,
                claimed.execution_id,
                status="failed",
                error="automation worker was stopped",
            )
            raise
        except Exception as exc:
            result = AutomationRunResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        await asyncio.to_thread(
            self.store.complete,
            claimed.execution_id,
            status=result.status,
            result_summary=result.summary,
            error=result.error,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
        )
        return True

    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            handled = await self.run_once()
            if handled:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


__all__ = ["AutomationRunResult", "LocalAutomationWorker"]
