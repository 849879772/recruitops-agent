"""Authenticated local resume-extension application registration."""
from hashlib import sha256
import json
from secrets import compare_digest
from datetime import datetime, timezone
import os
import re
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from packages.config import get_settings
from packages.storage import Storage, ApplicationSnapshot, JobSnapshot
from packages.domain.urls import normalize_http_page_url
from packages.browser_bridge import BrowserBridgeStore, OperationName, OperationStatus
from packages.tools.application_status_evidence import (
    VerifyApplicationStatusEvidenceInput, verify_application_status_evidence,
)
from packages.tools.browser_status_update import _normalise_status

router = APIRouter(prefix="/api/integrations/resume-filler")


class Registration(BaseModel):
    application_id: str | None = Field(default=None, max_length=255)
    job_id: str | None = Field(default=None, max_length=255)
    company: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=512)
    record_url: str = Field(max_length=2048)
    city: str = Field(default="", max_length=255)
    progress_url_confirmed: bool = False

    @field_validator("company", "title")
    @classmethod
    def nonempty(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Company and title are required")
        return value

    @field_validator("job_id")
    @classmethod
    def valid_job_id(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Job ID cannot be empty")
        return value

    @field_validator("record_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip()
        parsed = urlsplit(value)
        if normalize_http_page_url(value) is None or re.search(r"[\s\\\x00-\x1f]", value):
            raise ValueError("A recruitment page URL is required")
        route = unquote(parsed.path.rstrip("/") + "/" + parsed.fragment).lower()
        detail_route = re.sub(r"/position/application/?(?=$|#)", "/applications/", route)
        if re.search(r"(?:^|[/#!_-])(?:job|jobs|position|positions|jobdetail|job-detail|detail|apply)(?:[/.?!_-]|$)", detail_route):
            raise ValueError("这是岗位详情页，请填写官网的我的投递/投递记录页面地址")
        return value

    @model_validator(mode="after")
    def require_progress_confirmation(self):
        parsed = urlsplit(self.record_url)
        route = unquote(parsed.path + "/" + parsed.fragment).lower()
        if not self.progress_url_confirmed and not re.search(
            r"(?:^|[/#!_-])(?:applications?|myapplications?|applicationrecords?|records?|progress|deliveries|投递记录)(?:[/.?!_-]|$)", route
        ):
            raise ValueError("Unknown progress URL requires explicit confirmation")
        return self


def authorized_settings(authorization, instance_id=None, *, write=False):
    settings = get_settings()
    if not settings.api_token or not compare_digest(authorization or "", "Bearer " + settings.api_token):
        raise HTTPException(401, "Local API token is required")
    if getattr(settings, "env", None) == "desktop-isolated":
        expected = os.environ.get("RECRUITOPS_DESKTOP_INSTANCE_ID", "")
        if not expected or not compare_digest(instance_id or "", expected):
            raise HTTPException(409, "desktop_instance_mismatch")
        if write and os.environ.get("RECRUITOPS_DESKTOP_WRITE_OPTIN") != expected:
            raise HTTPException(403, "desktop_write_optin_required")
    if write and not settings.write_enabled:
        raise HTTPException(403, "Writes are disabled")
    return settings


@router.get("/applications")
def applications(authorization: str | None = Header(default=None),
                 x_recruitops_instance_id: str | None = Header(default=None)):
    settings = authorized_settings(authorization, x_recruitops_instance_id)
    with Storage.from_url(settings.database_url).session() as session:
        rows = session.execute(select(ApplicationSnapshot, JobSnapshot.detail_url)
            .outerjoin(JobSnapshot, ApplicationSnapshot.job_id == JobSnapshot.id)
            .order_by(ApplicationSnapshot.updated_at.desc())).all()
        return {"items": [{"id": row.id, "job_id": row.job_id,
            "company": row.company_name, "title": row.job_title,
            "record_url": row.record_url, "detail_url": detail_url, "stage": row.stage}
            for row, detail_url in rows]}

@router.post("/application")
def register(body: Registration, authorization: str | None = Header(default=None),
             x_recruitops_instance_id: str | None = Header(default=None)):
    settings = authorized_settings(authorization, x_recruitops_instance_id, write=True)
    identity_source = (
        body.company + "\njob-id:" + body.job_id
        if body.job_id is not None
        else body.company + "\n" + body.title
    )
    identity = sha256(identity_source.encode()).hexdigest()
    storage = Storage.from_url(settings.database_url)
    with storage.write_transaction() as session:
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            session.execute(text("select pg_advisory_xact_lock(:key)"), {"key": int(identity[:15], 16)})
        query = select(ApplicationSnapshot).with_for_update()
        if body.application_id:
            query = query.where(ApplicationSnapshot.id == body.application_id)
        elif body.job_id is not None:
            query = query.where(
                ApplicationSnapshot.company_name == body.company,
                ApplicationSnapshot.job_id == body.job_id,
            )
        else:
            query = query.where(ApplicationSnapshot.company_name == body.company, ApplicationSnapshot.job_title == body.title)
        matches = list(session.scalars(query))
        if body.application_id and not matches:
            raise HTTPException(404, "Application not found")
        if len(matches) > 1:
            raise HTTPException(409, "Multiple applications match; choose the record in Agent")
        row = matches[0] if matches else None
        if row and (
            row.company_name != body.company
            or row.job_title != body.title
            or (body.job_id is not None and row.job_id != body.job_id)
        ):
            raise HTTPException(409, "Selected application identity changed; refresh and select again")
        created = row is None
        if created:
            now = datetime.now(timezone.utc).isoformat()
            row = ApplicationSnapshot(id="resume-" + identity[:24], company_name=body.company,
                job_title=body.title, job_id=body.job_id, record_url=body.record_url, stage="applied",
                idempotency_key="resume-filler:" + identity, source="resume_filler", source_ref=identity,
                note=body.city, stage_history=[{"stage": "applied", "source": "resume_filler", "at": now}])
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                # A concurrent registration may have committed the same identity.
                row = session.get(ApplicationSnapshot, "resume-" + identity[:24])
                if row is None:
                    raise
                if (row.company_name != body.company or row.job_title != body.title
                        or row.job_id != body.job_id):
                    raise HTTPException(409, "Registration identity changed during concurrent save")
                created = False
        elif (body.application_id or not row.record_url) and row.record_url != body.record_url:
            row.record_url = body.record_url
            row.updated_at = datetime.now(timezone.utc)
        return {"ok": True, "application_id": row.id, "created": created, "current_stage": row.stage,
                "total": session.scalar(select(func.count()).select_from(ApplicationSnapshot))}


class StatusSync(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    application_id: str = Field(min_length=1, max_length=255)
    observation_operation_id: str = Field(min_length=1, max_length=128)
    page_url: str = Field(min_length=1, max_length=2048)


class LocalObservationSync(BaseModel):
    """Desktop-main-only normalized observation; it never accepts a target stage."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    application_id: str = Field(min_length=1, max_length=255)
    page_url: str = Field(min_length=1, max_length=2048)
    observation: dict

    @model_validator(mode="after")
    def bounded_observation(self):
        if len(json.dumps(self.observation, ensure_ascii=False).encode("utf-8")) > 262144:
            raise ValueError("observation_too_large")
        return self


class LocalBatchObservationSync(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    application_ids: list[str] = Field(min_length=1, max_length=50)
    page_url: str = Field(min_length=1, max_length=2048)
    observation: dict

    @field_validator("application_ids")
    @classmethod
    def unique_application_ids(cls, value):
        if len(value) != len(set(value)) or any(not item or len(item) > 255 for item in value):
            raise ValueError("application_ids must be unique and nonempty")
        return value

    @model_validator(mode="after")
    def bounded_observation(self):
        if len(json.dumps(self.observation, ensure_ascii=False).encode("utf-8")) > 262144:
            raise ValueError("observation_too_large")
        return self


def _persist_local_observation(body: LocalObservationSync | LocalBatchObservationSync, application_ids, authorization,
                               x_recruitops_instance_id):
    settings = authorized_settings(authorization, x_recruitops_instance_id, write=True)
    envelope = body.observation
    operation_id = envelope.get("operation_id")
    result = envelope.get("result")
    page_url = normalize_http_page_url(body.page_url)
    if (envelope.get("protocol_version") != 1 or envelope.get("type") != "result" or
            envelope.get("status") != "SUCCEEDED" or not isinstance(operation_id, str) or
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", operation_id) or not isinstance(result, dict)):
        raise HTTPException(409, "normalized_observation_required")
    if (result.get("evidence_only") is not True or result.get("database_updated") is not False or
            normalize_http_page_url(str(result.get("page_url") or "")) != page_url or
            not isinstance(result.get("application_ids"), list) or
            any(application_id not in result["application_ids"] for application_id in application_ids)):
        raise HTTPException(409, "observation_binding_mismatch")
    records = result.get("application_records")
    if not isinstance(records, list) or len(records) > 100:
        raise HTTPException(409, "structured_observation_required")
    # Store the normalized, page-bound desktop observation in the same evidence ledger
    # consumed by the existing verifier. No stage is accepted from this endpoint.
    store = BrowserBridgeStore(Storage.from_url(settings.database_url))
    instance_value = x_recruitops_instance_id or "local"
    device_id = "desktop-local-" + instance_value
    idem = "local-observation-" + sha256((instance_value + operation_id).encode()).hexdigest()
    command = {"action": "observe_application_page", "selector_key": "application_page",
               "application_id": application_ids[0], "application_ids": application_ids,
               "application_url": page_url, "page_url": page_url}
    try:
        operation = store.create(OperationName.OBSERVE_APPLICATION_STATUS_PAGE, device_id=device_id,
                                 idempotency_key=idem, operation_id=operation_id, command=command)
        if operation.status == OperationStatus.CONNECTING.value:
            store.ack(device_id, operation.last_outbox_sequence, operation_id=operation_id,
                      ack_id="desktop-local-ack-" + sha256(operation_id.encode()).hexdigest()[:24])
            store.append_event(operation_id, "desktop-local-extract-" + sha256(operation_id.encode()).hexdigest()[:24],
                               OperationStatus.EXTRACTING, sequence=1, payload={"source": "desktop_owned_page"})
            store.append_event(operation_id, "desktop-local-validate-" + sha256(operation_id.encode()).hexdigest()[:24],
                               OperationStatus.VALIDATING, sequence=2, payload={"normalized": True})
            store.terminal_result(operation_id, result, status=OperationStatus.SUCCEEDED,
                                  event_id=str(envelope.get("event_id") or "desktop-local-terminal"), sequence=3)
    except (KeyError, ValueError) as exc:
        raise HTTPException(409, "observation_persistence_conflict") from exc
    return operation_id


@router.post("/sync-local-observation")
def sync_local_observation(body: LocalObservationSync,
                           authorization: str | None = Header(default=None),
                           x_recruitops_instance_id: str | None = Header(default=None)):
    operation_id = _persist_local_observation(body, [body.application_id], authorization,
                                              x_recruitops_instance_id)
    return sync_status(StatusSync(application_id=body.application_id,
                                  observation_operation_id=operation_id, page_url=body.page_url),
                       authorization, x_recruitops_instance_id)


@router.post("/sync-local-observations")
def sync_local_observations(body: LocalBatchObservationSync,
                            authorization: str | None = Header(default=None),
                            x_recruitops_instance_id: str | None = Header(default=None)):
    operation_id = _persist_local_observation(body, body.application_ids, authorization,
                                              x_recruitops_instance_id)
    results = []
    for application_id in body.application_ids:
        try:
            result = sync_status(StatusSync(application_id=application_id,
                                            observation_operation_id=operation_id, page_url=body.page_url),
                                 authorization, x_recruitops_instance_id)
            results.append({"application_id": application_id, "success": result.get("success") is True})
        except HTTPException as exc:
            results.append({"application_id": application_id, "success": False, "reason": str(exc.detail)})
    return {"results": results}


@router.post("/sync")
def sync_status(body: StatusSync, authorization: str | None = Header(default=None),
                x_recruitops_instance_id: str | None = Header(default=None)):
    settings = authorized_settings(authorization, x_recruitops_instance_id, write=True)
    store = BrowserBridgeStore(Storage.from_url(settings.database_url))
    operation = store.get_operation(body.observation_operation_id)
    if operation is None or operation.status != "SUCCEEDED":
        raise HTTPException(409, "fresh_completed_observation_required")
    result = operation.result or {}
    page_url = normalize_http_page_url(body.page_url)
    if not page_url or page_url != normalize_http_page_url(str(result.get("page_url") or "")):
        raise HTTPException(409, "observation_page_mismatch")
    try:
        captured = datetime.fromisoformat(str(result.get("captured_at", "")).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - captured).total_seconds()
        if not 0 <= age <= 300:
            raise ValueError("stale")
    except (ValueError, TypeError):
        raise HTTPException(409, "observation_expired_reobserve")
    with store.storage.session() as session:
        row = session.get(ApplicationSnapshot, body.application_id)
        if row is None:
            raise HTTPException(404, "Application not found")
        if normalize_http_page_url(row.record_url or "") != page_url:
            raise HTTPException(409, "application_page_mismatch")
        cards = [card for card in result.get("application_records", [])
                 if isinstance(card, dict) and card.get("title") == row.job_title
                 and card.get("application_id", row.id) == row.id
                 and card.get("company", row.company_name) == row.company_name]
    if len(cards) != 1:
        raise HTTPException(409, "unique_target_card_required")
    card = cards[0]
    status = _normalise_status(card.get("status"))
    label = card.get("label") or card.get("status")
    evidence = card.get("evidence") or card.get("context")
    if not status or not label or not evidence:
        raise HTTPException(409, "structured_status_evidence_required")
    try:
        request = VerifyApplicationStatusEvidenceInput(
            application_id=body.application_id,
            observation_operation_id=body.observation_operation_id,
            observed_status=status, observed_label=label, evidence=evidence,
            confidence=card.get("confidence", 0), captured_at=captured,
        )
    except ValidationError:
        raise HTTPException(409, "structured_status_evidence_invalid")
    return verify_application_status_evidence(request, store).model_dump(mode="json")
