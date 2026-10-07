from __future__ import annotations

import asyncio
import errno
import inspect
import re
from collections.abc import Awaitable, Callable
from typing import Any

from packages.security.boundaries import redact_sensitive


class AutomationStartupError(RuntimeError):
    """A bounded startup failure, before any business turn was submitted."""

    error_code = "automation_startup_unavailable"

    def __init__(self, *, attempts: int, detail: str) -> None:
        self.attempts = attempts
        self.detail = detail
        super().__init__(
            f"定时任务未启动：助理工具连接失败，已尝试 {attempts} 次。{detail}"
        )


_PERMANENT_FAILURE = re.compile(
    r"\b(?:401|403|unauthori[sz]ed|forbidden|permission denied)\b|"
    r"(?:invalid|missing|expired|incorrect)[\s_-]*(?:api[\s_-]*key|token|credentials)|"
    r"(?:invalid|missing|unknown|unsupported)[\s_-]*(?:config(?:uration)?|provider|model)|"
    r"(?:executable|command)[^\n]{0,40}(?:not found|does not exist)|"
    r"(?:配置错误|配置无效|缺少配置|认证失败|鉴权失败|密钥无效)",
    re.IGNORECASE,
)
_TRANSIENT_FAILURE = re.compile(
    r"\b(?:timeout|timed out|connection (?:refused|reset|aborted)|"
    r"temporarily unavailable|temporary failure|server unavailable|"
    r"service unavailable|502|503|504)\b|"
    r"(?:连接超时|握手超时|连接被拒绝|连接重置|暂时不可用|临时连接故障)",
    re.IGNORECASE,
)
_TRANSIENT_ERRNOS = {
    errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
    errno.ETIMEDOUT, errno.ENETUNREACH, errno.EHOSTUNREACH,
}


def _retryable_startup_error(exc: Exception) -> bool:
    # Authentication and configuration failures cannot improve by waiting,
    # even if a provider adds a timeout phrase to the same error message.
    detail = str(exc)
    if str(getattr(exc, "code", "")) in {"401", "403"} or _PERMANENT_FAILURE.search(detail):
        return False
    if isinstance(exc, (ValueError, TypeError, FileNotFoundError, PermissionError)):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in _TRANSIENT_ERRNOS or getattr(exc, "winerror", None) in {
            10051, 10053, 10054, 10060, 10061, 10065,
        }
    return bool(_TRANSIENT_FAILURE.search(detail))


def _safe_error(exc: Exception) -> str:
    detail = str(redact_sensitive(f"{type(exc).__name__}: {exc}"))
    return detail[:1024] if str(exc) else f"{type(exc).__name__}: 助理工具连接超时"


async def start_automation_thread(
    service: Any,
    *,
    on_retry: Callable[..., Any] | None = None,
    max_attempts: int = 3,
    sleeper: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    startup_timeout_seconds: float = 75.0,
) -> Any:
    """Retry only thread startup; this function never submits a business turn."""

    if not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3")
    if startup_timeout_seconds <= 0:
        raise ValueError("startup_timeout_seconds must be positive")
    delays = (2.0, 5.0)
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.wait_for(
                service.thread_start(), timeout=startup_timeout_seconds
            )
        except Exception as exc:
            if not _retryable_startup_error(exc):
                raise
            error = _safe_error(exc)
            if attempt == max_attempts:
                raise AutomationStartupError(attempts=attempt, detail=error) from None
            if on_retry is not None:
                callback = on_retry(attempt=attempt, max_attempts=max_attempts, error=error)
                if inspect.isawaitable(callback):
                    await callback
            await sleeper(delays[attempt - 1])


__all__ = ["AutomationStartupError", "start_automation_thread"]
