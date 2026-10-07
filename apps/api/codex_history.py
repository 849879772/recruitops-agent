"""Safe, actionable errors for reading/restoring chat history (never start a turn)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from secrets import token_hex
import traceback

from fastapi import HTTPException

from packages.codex_runtime.client import JsonRpcClientError, JsonRpcRemoteError

logger = logging.getLogger(__name__)
HISTORY_REQUEST_TIMEOUT_SECONDS = 45


def history_http_error(error: Exception, *, operation: str, thread_id: str) -> HTTPException:
    """Do not expose/log raw RPC text: it can contain paths and message contents."""
    raw = str(error).casefold()
    status, code, message = 500, "history_internal_error", "会话服务处理失败，请重试；已有聊天记录未删除。"
    if isinstance(error, TimeoutError):
        status, code, message = 504, "history_timeout", "会话读取或恢复超时，请稍后重试。"
    elif isinstance(error, FileNotFoundError) or (
        isinstance(error, JsonRpcRemoteError)
        and any(marker in raw for marker in (
            "no rollout found", "rollout path", "thread not found", "unknown thread",
            "no such file", "os error 2", "cannot find the file", "not materialized",
        ))
    ):
        status, code, message = 404, "history_not_found", "未找到这条会话的历史文件。已显示的消息会保留，请检查程序数据目录是否完整。"
    elif "limit" in raw and any(marker in raw for marker in ("separator", "buffer", "json-rpc message")):
        status, code, message = 413, "history_too_large", "会话历史超过单次读取上限，已有消息会保留；可新建会话继续。"
    elif isinstance(error, (ConnectionError, OSError)) or (
        isinstance(error, JsonRpcClientError) and not isinstance(error, JsonRpcRemoteError)
    ):
        status, code, message = 503, "history_runtime_unavailable", "助理运行时暂不可用，请待服务就绪后重试恢复。"
    elif isinstance(error, JsonRpcRemoteError):
        status, code, message = 502, "history_runtime_error", "助理运行时拒绝了会话请求，请重试恢复；不会重新执行历史任务。"
    request_id = token_hex(6)
    frames = ";".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                      for frame in traceback.extract_tb(error.__traceback__)[-4:])
    logger.warning(
        "conversation operation=%s code=%s request_id=%s thread_ref=%s exception=%s frames=%s",
        operation, code, request_id, hashlib.sha256(thread_id.encode()).hexdigest()[:12],
        type(error).__name__, frames,
    )
    return HTTPException(status_code=status, detail={
        "code": code, "message": message, "request_id": request_id,
    })
