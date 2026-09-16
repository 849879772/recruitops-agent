from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .supervisor import CodexSupervisor
from .instructions import with_response_language


class ThreadRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)


class TurnRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)


class ThreadPage(BaseModel):
    """Version-stable projection of ``thread/list`` pagination."""

    model_config = ConfigDict(extra="allow")

    data: list[ThreadRef] = Field(default_factory=list)
    next_cursor: str | None = None


class CodexThreads:
    """Version-pinned high-level thread/turn calls over the App Server client."""

    def __init__(self, supervisor: CodexSupervisor) -> None:
        self._supervisor = supervisor

    async def start(self, **params: Any) -> ThreadRef:
        response = await self._supervisor.request("thread/start", with_response_language(params))
        return ThreadRef.model_validate(_entity(response, "thread"))

    async def resume(self, thread_id: str, **params: Any) -> ThreadRef:
        response = await self._supervisor.request(
            "thread/resume", with_response_language({"threadId": thread_id, **params})
        )
        return ThreadRef.model_validate(_entity(response, "thread"))

    async def read(self, thread_id: str, *, include_turns: bool = True) -> ThreadRef:
        response = await self._supervisor.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
        )
        return ThreadRef.model_validate(_entity(response, "thread"))

    async def list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 20,
        archived: bool = False,
    ) -> ThreadPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        params: dict[str, Any] = {"limit": limit, "archived": archived}
        if cursor:
            params["cursor"] = cursor
        response = await self._supervisor.request("thread/list", params)
        if not isinstance(response, dict):
            raise ValueError("App Server response is missing thread list")
        rows = response.get("data", response.get("threads", []))
        if not isinstance(rows, list):
            raise ValueError("App Server thread list is invalid")
        next_cursor = response.get("nextCursor", response.get("next_cursor"))
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("App Server thread cursor is invalid")
        return ThreadPage(
            data=[ThreadRef.model_validate(row) for row in rows],
            next_cursor=next_cursor,
        )

    async def start_turn(
        self,
        thread_id: str,
        text: str,
        **params: Any,
    ) -> TurnRef:
        response = await self._supervisor.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                **params,
            },
        )
        return TurnRef.model_validate(_entity(response, "turn"))

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        await self._supervisor.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )

    async def delete(self, thread_id: str) -> None:
        await self._supervisor.request(
            "thread/delete", {"threadId": thread_id}
        )


def _entity(response: Any, key: str) -> Any:
    if not isinstance(response, dict) or not isinstance(response.get(key), dict):
        raise ValueError(f"App Server response is missing {key!r}")
    return response[key]


__all__ = ["CodexThreads", "ThreadPage", "ThreadRef", "TurnRef"]
