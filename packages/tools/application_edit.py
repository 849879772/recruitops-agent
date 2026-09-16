"""Narrow, user-directed correction of application metadata, never its stage."""
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field, model_validator
from sqlalchemy import select
from packages.storage import ApplicationSnapshot
from .typed import ToolInput, ToolResponse, ToolStatus, ToolErrorCode, EvidenceSource


class ApplicationEditInput(ToolInput):
    application_id: str = Field(min_length=1, max_length=255)
    expected_updated_at: datetime
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    job_title: str | None = Field(default=None, min_length=1, max_length=512)
    record_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_patch(self):
        fields = self.model_fields_set & {"company_name", "job_title", "record_url"}
        if not fields or self.expected_updated_at.tzinfo is None:
            raise ValueError("Supply changed fields and a timezone-aware current version")
        if any(getattr(self, k) is None for k in fields - {"record_url"}):
            raise ValueError("Company and job title cannot be null")
        if self.record_url is not None:
            url = urlsplit(self.record_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("Record URL must be an HTTP(S) page without credentials")
        return self


class ApplicationEditResponse(ToolResponse[dict]):
    read_only: Literal[False] = False


def edit_application_metadata(request, storage, *, write_enabled):
    def response(data=None, error=None, message=None):
        return ApplicationEditResponse(tool_name="application_edit", success=error is None,
            status=ToolStatus.SUCCESS if error is None else ToolStatus.FAILURE,
            data=data, error_code=error, error_message=message,
            evidence=[EvidenceSource(source="local_application", source_ref=request.application_id)],
            timeout_ms=request.timeout_ms, elapsed_ms=0)
    if not write_enabled:
        return response(error=ToolErrorCode.READ_ONLY_VIOLATION, message="本地写入已关闭")
    if storage is None:
        return response(error=ToolErrorCode.SOURCE_UNAVAILABLE, message="投递记录存储不可用")
    try:
        with storage.write_transaction() as session:
            row = session.scalar(select(ApplicationSnapshot).where(
                ApplicationSnapshot.id == request.application_id).with_for_update())
            if row is None:
                return response(error=ToolErrorCode.NOT_FOUND, message="投递记录不存在")
            fields = request.model_fields_set & {"company_name", "job_title", "record_url"}
            changes = {k: getattr(request, k) for k in fields if getattr(row, k) != getattr(request, k)}
            if changes and row.updated_at.replace(tzinfo=timezone.utc) != request.expected_updated_at.astimezone(timezone.utc):
                return response(error=ToolErrorCode.INVALID_INPUT, message="记录已变化，请重新查询后修改")
            before = {k: getattr(row, k) for k in changes}
            for key, value in changes.items():
                setattr(row, key, value)
            if changes:
                row.updated_at = datetime.now(timezone.utc)
            session.flush()
            return response({"application_id": row.id, "changed": bool(changes), "before": before,
                             "after": changes, "updated_at": row.updated_at.isoformat(),
                             "stage": row.stage, "stage_history_changed": False})
    except Exception:
        return response(error=ToolErrorCode.SOURCE_UNAVAILABLE, message="投递记录修改失败，未完成提交")
