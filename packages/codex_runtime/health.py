from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .supervisor import CodexSupervisor, SupervisorState


class RuntimeHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    state: SupervisorState
    detail: str


async def runtime_health(supervisor: CodexSupervisor) -> RuntimeHealth:
    status = await supervisor.health()
    return RuntimeHealth(
        ready=status.healthy,
        state=status.state,
        detail="initialized" if status.healthy else (status.error or "not initialized"),
    )


__all__ = ["RuntimeHealth", "runtime_health"]
