"""Persist scheduled-task conversations without asking a model to run them twice."""

from datetime import datetime, timezone

from sqlalchemy import select

from packages.storage import ConversationMessage, ConversationThread


def record_started(session, execution, schedule, *, direct: bool, local_only: bool = False) -> None:
    thread = session.get(ConversationThread, execution.thread_id)
    if thread is not None:
        return
    timestamp = execution.started_at or datetime.now(timezone.utc)
    metadata = {
        "execution_id": execution.id, "schedule_id": execution.schedule_id,
        "task_id": schedule.task_id, "run_id": execution.id if direct else None,
        "thread_id": execution.thread_id, "direct": direct, "status": "running",
    }
    if local_only:
        metadata["local_only"] = True
    thread = ConversationThread(id=execution.thread_id, title=f"定时任务 · {schedule.task_label}"[:255],
        context={"automation": metadata}, created_at=timestamp, updated_at=timestamp)
    session.add(thread)
    session.flush()
    scope = schedule.target_label or ("全部目标" if schedule.target_kind == "all" else schedule.target_id)
    session.add(ConversationMessage(id=f"{execution.id}:request", thread_id=thread.id,
        role="user", body=f"执行定时任务：{schedule.task_label}。范围：{scope or '已保存范围'}。",
        task_id=execution.id, result=metadata, sequence_no=0, created_at=timestamp))
    session.add(ConversationMessage(id=f"{execution.id}:started", thread_id=thread.id,
        role="assistant", body="定时任务已创建，正在准备执行；进度和结果会保留在本会话。",
        task_id=execution.id, result=metadata, sequence_no=1, created_at=timestamp))


def record_completed(session, execution, *, details=None) -> None:
    if not execution.thread_id:
        return
    thread = session.get(ConversationThread, execution.thread_id)
    if thread is None or not (thread.context or {}).get("automation"):
        return
    metadata = {**thread.context["automation"], "status": execution.status,
                "turn_id": execution.turn_id, "error": execution.error}
    thread.context = {**thread.context, "automation": metadata}
    thread.updated_at = execution.completed_at
    message_id = f"{execution.id}:completed"
    if session.get(ConversationMessage, message_id) is None:
        body = execution.result_summary or (
            "定时任务已完成。" if execution.status == "succeeded" else "定时任务未完成。")
        if execution.error and execution.error not in body:
            body += f"\n{execution.error}"
        sequence_no = max(session.scalars(select(ConversationMessage.sequence_no).where(
            ConversationMessage.thread_id == thread.id)), default=1) + 1
        session.add(ConversationMessage(id=message_id, thread_id=thread.id, role="assistant",
            body=body, task_id=execution.id, result={**metadata, "details": details}, sequence_no=sequence_no,
            created_at=execution.completed_at))


def record_startup_retry(session, execution, *, attempt: int, max_attempts: int, error: str) -> None:
    """Replace startup progress in one message rather than growing the chat per retry."""
    from packages.security.boundaries import redact_sensitive

    detail = redact_sensitive(error)[:1000]
    reason = "工具连接启动超时" if "timeout" in error.casefold() or "timed out" in error.casefold() else "助理连接暂未就绪"
    summary = f"{reason}，准备重试（{attempt}/{max_attempts}）。"
    execution.result_summary = summary
    execution.error = detail
    if not execution.thread_id:
        return
    thread = session.get(ConversationThread, execution.thread_id)
    if thread is None:
        return
    timestamp = datetime.now(timezone.utc)
    metadata = {**thread.context["automation"], "startup_attempt": attempt,
                "startup_max_attempts": max_attempts, "startup_error": detail}
    thread.context = {**thread.context, "automation": metadata}
    thread.updated_at = timestamp
    message_id = f"{execution.id}:startup-retry"
    message = session.get(ConversationMessage, message_id)
    if message is None:
        message = ConversationMessage(id=message_id, thread_id=thread.id, role="assistant",
            body=summary, task_id=execution.id, result=metadata, sequence_no=2, created_at=timestamp)
        session.add(message)
    else:
        message.body = summary
        message.result = metadata
        message.created_at = timestamp


def bind_followup_thread(storage, thread_id: str, runtime_thread_id: str) -> None:
    with storage.transaction(write=True) as session:
        thread = session.get(ConversationThread, thread_id)
        if thread is not None and (thread.context or {}).get("automation", {}).get("local_only"):
            thread.context = {**thread.context, "automation": {
                **thread.context["automation"], "followup_thread_id": runtime_thread_id}}


def followup_context(payload) -> str:
    """Carry reports, never replay the saved task request as a new instruction."""
    automation = payload["automation"]
    reports = "\n".join(row["text"] for row in payload.get("messages", []) if row["role"] == "assistant")[-8000:]
    return ("[既有定时任务记录，仅供解释用户追问；任务请求已经执行，不得因为加载记录再次启动任务。"
            "如需最新状态，按原 run_id 查询；只有用户明确要求重新执行时才启动新任务。]\n"
            f"任务：{payload['title']}\n状态：{automation.get('status')}\n"
            f"run_id：{automation.get('run_id') or automation.get('execution_id')}\n{reports}\n\n")


def _timestamp(value):
    return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).timestamp()


def thread_updated_at(payload):
    value = payload.get("updatedAt") or payload.get("updated_at") or 0
    try:
        numeric = float(value)
        return numeric / 1000 if numeric >= 1e12 else numeric
    except (TypeError, ValueError):
        try:
            return _timestamp(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except ValueError:
            return 0


def _payload(thread, messages=()):
    return {
        "id": thread.id, "title": thread.title, "preview": thread.title,
        "createdAt": _timestamp(thread.created_at), "updatedAt": _timestamp(thread.updated_at),
        "automation": dict(thread.context["automation"]),
        "messages": [{"id": row.id, "role": row.role, "text": row.body,
                      "createdAt": _timestamp(row.created_at), "task_id": row.task_id,
                      "result": row.result} for row in messages],
    }


def read_conversation(storage, thread_id, *, include_messages=True):
    with storage.session() as session:
        thread = session.get(ConversationThread, thread_id)
        if thread is None or not (thread.context or {}).get("automation") or thread.context.get("archived"):
            return None
        messages = list(session.scalars(select(ConversationMessage).where(
            ConversationMessage.thread_id == thread_id).order_by(ConversationMessage.sequence_no))) if include_messages else []
        return _payload(thread, messages)


def list_conversations(storage, *, limit=100):
    with storage.session() as session:
        threads = session.scalars(select(ConversationThread).where(
            ConversationThread.context["archived"].as_boolean().is_not(True)).order_by(
            ConversationThread.updated_at.desc()).limit(limit))
        return [_payload(thread) for thread in threads if (thread.context or {}).get("automation")]


def archive_conversation(storage, thread_id):
    with storage.transaction(write=True) as session:
        thread = session.get(ConversationThread, thread_id)
        if thread is not None and (thread.context or {}).get("automation"):
            thread.context = {**thread.context, "archived": True}


def visible_runtime_conversations(storage, rows):
    ids = [row["id"] for row in rows]
    if not ids:
        return rows
    with storage.session() as session:
        hidden = set(session.scalars(select(ConversationThread.id).where(
            ConversationThread.id.in_(ids), ConversationThread.context["archived"].as_boolean().is_(True))))
    return [row for row in rows if row["id"] not in hidden]
