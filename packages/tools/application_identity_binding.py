"""Human-only, page-scoped identity confirmation; never a status-write shortcut."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from sqlalchemy import or_, select, update

from packages.approval.models import ApprovalDecision, ApprovalPreview, EvidenceRef, OperationName
from packages.approval.executor import WriteEffect
from packages.domain.urls import normalize_http_page_url
from packages.domain.application_identity import matching_records, title_key
from packages.domain.application_evidence_text import localize_evidence
from packages.storage.models import ApplicationSnapshot, ApplicationIdentityBinding, BrowserOperation, BrowserOperationEvent, TaskRun, ToolCall
from packages.storage.application_reviews import IDENTITY_REASONS
from packages.tools.application_page_evidence import identity_fallback_cards
from packages.tools.typed import EvidenceSource, ToolInput, ToolResponse, ToolStatus, ToolErrorCode


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _identity(application):
    return {key: _get(application, key) for key in ("id", "company_name", "job_title", "job_id", "record_url")}


def _application_closed(application):
    return str(_get(application, "stage", "") or "").casefold() in {"rejected", "withdrawn"}


def _revision(binding):
    return binding.revision if binding else 0


def _card_identity(card):
    return {key: card[key] for key in ("raw_title", "external_application_id", "external_job_id") if card.get(key)}


def _card_identity_key(card):
    identity = _card_identity(card)
    identity["raw_title"] = title_key(identity.get("raw_title"))
    return _digest(identity)


def _merge_identity_cards(cards, fallback):
    """Merge independent sources, not separate same-title applications."""
    dom_titles, visual_titles = {}, {}
    for collection, counts in ((cards, dom_titles), (fallback, visual_titles)):
        for card in collection:
            key = title_key(card.get("raw_title") or card.get("title"))
            counts[key] = counts.get(key, 0) + 1
    merged = list(cards)
    for card in fallback:
        key = title_key(card.get("raw_title") or card.get("title"))
        same = [item for item in cards if title_key(item.get("raw_title") or item.get("title")) == key]
        duplicate = False
        if dom_titles.get(key) == visual_titles.get(key) == 1:
            first = str(same[0].get("context") or same[0].get("evidence") or "")
            second = str(card.get("context") or card.get("evidence") or "")
            # A shared title/status alone does not prove two captures show the
            # same application. Require a shared literal card body and preserve
            # all repeated cards within either source for manual resolution.
            duplicate = bool(first and second and min(len(title_key(first)), len(title_key(second))) > len(key) + 8
                             and (localize_evidence(first, second) or localize_evidence(second, first)))
        if not duplicate:
            merged.append(card)
    return merged


def _observation(session, application, operation_id=None, *, allow_expired=False):
    # Reuse the same capture/redirect binding as the status-write verifier.
    from packages.tools.application_status_evidence import _bound_observation_target
    page_url = normalize_http_page_url(application.record_url or "")
    if not page_url:
        raise ValueError("mail_only_application")
    query = select(BrowserOperation).where(
        BrowserOperation.operation == "observe_application_status_page",
        BrowserOperation.status == "SUCCEEDED")
    if operation_id:
        query = query.where(BrowserOperation.operation_id == operation_id)
    else:
        urls = list({application.record_url, page_url})
        query = query.where(or_(BrowserOperation.command["page_url"].as_string().in_(urls),
                                BrowserOperation.command["application_url"].as_string().in_(urls)))
    for operation in session.scalars(query.order_by(BrowserOperation.created_at.desc()).limit(100)):
        command = operation.command or {}
        ids = {str(value) for value in command.get("application_ids", [])}
        ids.add(str(command.get("application_id") or ""))
        if application.id not in ids or not isinstance(operation.result, dict):
            continue
        if _bound_observation_target(operation, command) != page_url:
            continue
        captured = operation.completed_at or operation.updated_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        if not allow_expired and datetime.now(timezone.utc) - captured > timedelta(hours=24):
            raise ValueError("observation_expired; review_the_application_again")
        cards = []
        for item in operation.result.get("application_records", []):
            if not isinstance(item, dict):
                continue
            title = str(item.get("raw_title") or item.get("title") or "").strip()
            if not title or len(title) > 512:
                continue
            card = {key: value for key, value in item.items() if not key.startswith("external_")}
            card["raw_title"] = title
            # Only these site-card fields are ATS identifiers. Command/application
            # snapshot IDs belong to a different namespace and are never aliases.
            for name in ("application_id", "job_id"):
                value = item.get(name)
                if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                    card["external_" + name] = str(value).strip()
            cards.append(card)
        reading = operation.result.get("vision")
        verified_vision = bool(reading and any(payload == reading for payload in session.scalars(
            select(BrowserOperationEvent.payload).where(
                BrowserOperationEvent.operation_id == operation.operation_id,
                BrowserOperationEvent.event_type == "vision_analysis"))))
        if verified_vision or not cards:
            fallback = identity_fallback_cards(operation.result, verified_vision=verified_vision)
            cards = _merge_identity_cards(cards, fallback)
        return operation, cards
    raise ValueError("observation_not_found; review_the_application_first")


def identity_candidates(storage, application_id, *, operation_id=None):
    with storage.session() as session:
        application = session.get(ApplicationSnapshot, application_id)
        if application is None:
            raise KeyError("application_not_found")
        binding = session.get(ApplicationIdentityBinding, application_id)
        result = {"application_id": application_id, "company_name": application.company_name,
                  "job_title": application.job_title, "page_url": application.record_url,
                  "identity_digest": _digest(_identity(application)), "binding_revision": _revision(binding),
                  "binding_state": binding.state if binding else "none",
                  "binding_valid": bool(binding and binding.state == "bound" and binding.approval_key and
                                        binding.identity_digest == _digest(_identity(application)) and
                                        binding.page_url == normalize_http_page_url(application.record_url or "")),
                  "current_title": binding.card.get("raw_title") if binding else None,
                  "requires_user_confirmation": True, "candidates": []}
        if _application_closed(application):
            result.update(requires_user_confirmation=False, unavailable_reason="application_closed")
            return result
        try:
            operation, cards = _observation(session, application, operation_id, allow_expired=True)
        except ValueError as exc:
            result["unavailable_reason"] = str(exc)
            return result
        result["operation_id"] = operation.operation_id
        captured_at = operation.completed_at or operation.updated_at
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        result["captured_at"] = captured_at.isoformat()
        target = _identity(application)
        if result["binding_valid"]:
            target["verified_identity_bindings"] = [{**binding.card, "application_id": application_id,
                                                     "page_url": binding.page_url, "verified": True}]
        matches = matching_records(target, cards)
        result["requires_user_confirmation"] = len(matches) != 1
        if not result["binding_valid"] and any(
            card.get("evidence_source") in {"vision", "page_text"} for card in matches
        ):
            result["requires_user_confirmation"] = True
        if datetime.now(timezone.utc) - captured_at > timedelta(hours=24):
            result["unavailable_reason"] = "observation_expired; review_the_application_again"
            return result
        if not cards:
            result["unavailable_reason"] = ("observation_evidence_expired" if operation.result.get("diagnostics_compacted_v1")
                                            else "application_records_missing")
        counts = {}
        for card in cards:
            key = _card_identity_key(card)
            counts[key] = counts.get(key, 0) + 1
        result["candidates"] = [{"candidate_id": _digest(card), "raw_title": card["raw_title"],
            "context": str(card.get("context") or card.get("evidence") or "")[:1500],
            "label": str(card.get("label") or "")[:200],
            "evidence_source": card.get("evidence_source", "dom"),
            "selectable": counts[_card_identity_key(card)] == 1,
            **{key: card[key] for key in ("external_application_id", "external_job_id") if card.get(key)}}
            for card in cards]
        return result


def application_identity_queue(storage):
    """Read the latest result for each application; historical receipts are audit only."""
    latest = {}
    with storage.session() as session:
        for application in session.scalars(select(ApplicationSnapshot).where(ApplicationSnapshot.last_review.is_not(None))):
            if application.last_review:
                latest[application.id] = application.last_review
        checkpoints = session.scalars(select(ToolCall).join(TaskRun, ToolCall.task_id == TaskRun.id).where(
            ToolCall.tool_name == "application_review_checkpoint").order_by(
                TaskRun.created_at.desc(), ToolCall.updated_at.desc(), ToolCall.id.desc()))
        for checkpoint in checkpoints:
            state = checkpoint.arguments or {}
            ids = {str(value) for value in state.get("ids", [])}
            for application_id, row in (state.get("results") or {}).items():
                if application_id in ids and application_id not in latest and isinstance(row, dict):
                    latest[application_id] = {**row, "run_id": checkpoint.task_id}
    items = []
    for application_id, row in latest.items():
        if row.get("state") != "unresolved" or row.get("reason") not in IDENTITY_REASONS:
            continue
        try:
            current = identity_candidates(storage, application_id)
        except KeyError:
            continue
        if not normalize_http_page_url(current.get("page_url") or "") or not current["requires_user_confirmation"]:
            continue
        items.append({**current, "reason": row["reason"], "run_id": row.get("run_id")})
    return {"items": items, "total": len(items), "read_only": True}


async def refresh_identity_candidates(storage, bridge, repository, application_id, *, request_id, vision_enabled=False):
    """Explicit user click: bounded evidence-only reread, never status verification."""
    from packages.tools.browser_bridge import ObserveApplicationStatusPageInput, observe_application_status_page_workflow

    current = identity_candidates(storage, application_id)
    if current.get("unavailable_reason") == "application_closed":
        raise ValueError("application_closed")
    if not normalize_http_page_url(current.get("page_url") or ""):
        raise ValueError("mail_only_application")
    key = "identity-reread-" + _digest([application_id, request_id])
    request = ObserveApplicationStatusPageInput(application_id=application_id,
        idempotency_key=key, timeout_ms=45_000, include_vision=False)
    for visual in (False, True):
        if visual:
            request = request.model_copy(update={"idempotency_key": key + "-vision", "include_vision": True,
                "vision_fallback_reason": "no_structured_evidence_visible_status_likely"})
        response = await observe_application_status_page_workflow(request, bridge, repository)
        if not response.success or not response.data or response.data.status != "SUCCEEDED":
            # The application can close while the browser request is in flight.
            if identity_candidates(storage, application_id).get("unavailable_reason") == "application_closed":
                raise ValueError("application_closed")
            code = ("timeout" if _get(response, "timed_out", False) else
                    _get(response.data, "error_code") or _get(response, "error_code"))
            raise ValueError(str(code or "identity_observation_failed"))
        operation_id = response.data.operation_id
        current = identity_candidates(storage, application_id, operation_id=operation_id)
        if current.get("unavailable_reason") == "application_closed":
            raise ValueError("application_closed")
        operation = bridge.get_operation(operation_id)
        observed = operation.result or {}
        diagnostics = observed.get("diagnostics") or {}
        readable = bool(str((observed.get("page") or {}).get("text") or "").strip())
        if (visual or current["candidates"] or not vision_enabled or not readable
                or diagnostics.get("loginPromptVisible") or diagnostics.get("captchaVisible")
                or diagnostics.get("scopeDeniedFrameCount") or diagnostics.get("unavailableFrameCount")):
            break
    return {**current, "stage_unchanged": True}


def _selected_card(session, application, operation_id, candidate_id):
    if _application_closed(application):
        raise ValueError("application_closed")
    latest, _ = _observation(session, application)
    if latest.operation_id != operation_id:
        raise ValueError("observation_changed; refresh_candidates")
    _, cards = _observation(session, application, operation_id)
    chosen = [card for card in cards if _digest(card) == candidate_id]
    if len(chosen) != 1:
        raise ValueError("candidate_changed_or_not_unique")
    identity = _card_identity(chosen[0])
    if sum(_card_identity_key(card) == _card_identity_key(chosen[0]) for card in cards) != 1:
        raise ValueError("candidate_identity_not_unique")
    return identity


def identity_binding_preview(storage, request):
    with storage.session() as session:
        application = session.get(ApplicationSnapshot, request.application_id)
        if application is None:
            raise KeyError("application_not_found")
        binding = session.get(ApplicationIdentityBinding, application.id)
        digest = _digest(_identity(application))
        if request.identity_digest != digest or request.binding_revision != _revision(binding):
            raise ValueError("application_or_binding_changed; refresh_candidates")
        card = _selected_card(session, application, request.operation_id, request.candidate_id) if request.action == "bind" else {}
        page_url = normalize_http_page_url(application.record_url or "") or ""
        payload = {"application_id": application.id, "identity_digest": digest, "page_url": page_url,
                   "expected_revision": _revision(binding), "action": request.action,
                   "operation_id": request.operation_id, "candidate_id": request.candidate_id, "card": card,
                   "confirmation_attempt": request.retry_of or "initial"}
        # Duplicate clicks/POSTs reuse a capability. Only an explicit retry of a
        # rejected/expired token creates another attempt; revision CAS still applies.
        key = "application-identity:" + _digest(payload)
        payload["approval_key"] = key
        before = {"company_name": application.company_name, "job_title": application.job_title,
                  "page_url": page_url, "binding_revision": _revision(binding),
                  "official_title": binding.card.get("raw_title") if binding else None}
        after = {"action": request.action, "official_title": card.get("raw_title"),
                 "binding_revision": _revision(binding) + 1, "stage_unchanged": True,
                 "official_job_id": card.get("external_job_id"),
                 "official_application_id": card.get("external_application_id")}
    now = datetime.now(timezone.utc)
    return ApprovalPreview(task_id=key, operation=OperationName.APPLICATION_IDENTITY_BINDING,
        idempotency_key=key, evidence_summary="Human confirms official record identity: " + _digest(payload),
        evidence=(EvidenceRef(source="browser_observation", source_ref=request.operation_id or request.application_id,
                              summary="Only identity confirmation; stage still requires fresh page evidence"),),
        target_id=request.application_id, payload=payload, before=before, after=after,
        created_at=now, expires_at=now + timedelta(minutes=15))


class ApplicationIdentityBindingAdapter:
    def __init__(self, storage):
        self.storage = storage

    def bind_application_identity(self, payload):
        with self.storage.write_transaction() as session:
            application = session.scalar(select(ApplicationSnapshot).where(
                ApplicationSnapshot.id == payload["application_id"]).with_for_update())
            if application is None or _digest(_identity(application)) != payload["identity_digest"]:
                raise ValueError("application_changed_after_preview")
            binding = session.get(ApplicationIdentityBinding, application.id)
            if binding and binding.approval_key == payload["approval_key"]:
                return WriteEffect(before={"revision": binding.revision}, after={"revision": binding.revision})
            if _revision(binding) != payload["expected_revision"]:
                raise ValueError("binding_changed_after_preview")
            if payload["action"] not in {"bind", "unbind"}:
                raise ValueError("invalid_binding_action")
            card = _selected_card(session, application, payload["operation_id"], payload["candidate_id"]) if payload["action"] == "bind" else {}
            if card != payload["card"]:
                raise ValueError("observation_changed_after_preview")
            before = {"revision": _revision(binding), "card": binding.card if binding else {}}
            values = {"revision": payload["expected_revision"] + 1,
                      "state": "bound" if card else "unbound", "identity_digest": payload["identity_digest"],
                      "page_url": payload["page_url"], "card": card, "operation_id": payload["operation_id"],
                      "approval_key": payload["approval_key"], "updated_at": datetime.now(timezone.utc)}
            if binding is None:
                session.add(ApplicationIdentityBinding(application_id=application.id, **values))
                session.flush()  # The primary key arbitrates concurrent first confirmations.
            else:
                saved = session.execute(update(ApplicationIdentityBinding).where(
                    ApplicationIdentityBinding.application_id == application.id,
                    ApplicationIdentityBinding.revision == payload["expected_revision"]).values(**values))
                if saved.rowcount != 1:
                    raise ValueError("binding_changed_after_preview")
            return WriteEffect(before=before, after={"revision": values["revision"],
                               "state": values["state"], "card": card, "stage_unchanged": True})


def hydrate_verified_identity_bindings(storage, applications):
    """Attach only DB-confirmed hints, invalidating them on any application edit.

    The private runtime attribute is not part of the public Application input/schema.
    Clear caller-provided hints even when there is no persisted confirmation.
    """
    applications = list(applications)
    def attach(application, value):
        if isinstance(application, dict):
            application["verified_identity_bindings"] = value
        else:
            object.__setattr__(application, "verified_identity_bindings", value)
    for application in applications:
        attach(application, [])
    if not applications:
        return applications
    ids = [str(_get(application, "id")) for application in applications]
    with storage.session() as session:
        bindings = {item.application_id: item for item in session.scalars(select(ApplicationIdentityBinding).where(
            ApplicationIdentityBinding.application_id.in_(ids), ApplicationIdentityBinding.state == "bound"))}
        for application in applications:
            binding = bindings.get(str(_get(application, "id")))
            if not binding or binding.identity_digest != _digest(_identity(application)) or not binding.approval_key:
                continue
            if binding.page_url != normalize_http_page_url(_get(application, "record_url") or ""):
                continue
            attach(application, [{**binding.card, "application_id": binding.application_id,
                                  "page_url": binding.page_url, "verified": True}])
    return applications


class ApplicationIdentityCandidatesInput(ToolInput):
    application_id: str = Field(min_length=1, max_length=255)
    operation_id: str | None = Field(default=None, max_length=128)


class ApplicationIdentityProposeInput(ApplicationIdentityCandidatesInput):
    action: Literal["bind", "unbind"] = "bind"
    identity_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_revision: int = Field(ge=0)
    candidate_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    retry_of: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_target(self):
        if self.action == "bind" and (not self.operation_id or not self.candidate_id):
            raise ValueError("binding requires a persisted observation and candidate")
        if self.action == "unbind" and (self.operation_id or self.candidate_id):
            raise ValueError("unbind has no new target")
        return self


class ApplicationIdentityCandidatesResponse(ToolResponse[dict[str, Any]]):
    pass


class ApplicationIdentityProposeResponse(ToolResponse[dict[str, Any]]):
    read_only: Literal[False] = False


def application_identity_candidates(request, storage):
    return ApplicationIdentityCandidatesResponse(tool_name="application_identity_candidates", status=ToolStatus.SUCCESS,
        success=True, data=identity_candidates(storage, request.application_id, operation_id=request.operation_id),
        timeout_ms=request.timeout_ms, elapsed_ms=0,
        evidence=[EvidenceSource(source="browser_observation", source_ref=request.application_id)])


def application_identity_propose(request, storage, registry):
    preview = identity_binding_preview(storage, request)
    now = datetime.now(timezone.utc)
    fields = ("application_id", "identity_digest", "page_url", "expected_revision", "action", "operation_id", "candidate_id", "card")
    related = [(token, saved) for token, saved in registry.queue()
               if saved.operation == OperationName.APPLICATION_IDENTITY_BINDING
               and all(saved.payload.get(key) == preview.payload.get(key) for key in fields)]
    live = next(((token, saved) for token, saved in reversed(related)
                 if token.status.value in {"pending", "approved"} and token.expires_at > now), None)
    if live:
        preview = live[1]
    elif request.retry_of:
        previous = next((token for token, _ in related if token.token_id == request.retry_of), None)
        if previous is None or not (previous.status.value in {"expired", "rejected"} or previous.expires_at <= now):
            raise ValueError("retry_requires_rejected_or_expired_proposal")
    elif related:
        token, preview = related[-1]
        if token.status.value in {"expired", "rejected"} or token.expires_at <= now:
            return ApplicationIdentityProposeResponse(tool_name="application_identity_propose",
                status=ToolStatus.FAILURE, success=False, timeout_ms=request.timeout_ms, elapsed_ms=0,
                error_code=ToolErrorCode.INVALID_INPUT, error_message="preview_requires_explicit_retry",
                data={"approval_id": token.token_id, "approval_status": "expired" if token.expires_at <= now else token.status.value,
                      "retry_of": token.token_id, "requires_user_confirmation": True, "business_write_performed": False,
                      "preview": preview.model_dump(mode="json")},
                evidence=[EvidenceSource(source="browser_observation", source_ref=request.application_id)])
    decision = (ApprovalDecision(allowed=True, status=live[0].status, token=live[0],
                                reason="The existing identity confirmation was reused.")
                if live else registry.issue(preview))
    if decision.token:
        preview = registry.preview(decision.token.token_id)
    return ApplicationIdentityProposeResponse(tool_name="application_identity_propose",
        status=ToolStatus.SUCCESS if decision.allowed else ToolStatus.FAILURE, success=decision.allowed,
        timeout_ms=request.timeout_ms, elapsed_ms=0,
        error_code=None if decision.allowed else ToolErrorCode.INVALID_INPUT,
        error_message=None if decision.allowed else decision.reason,
        data={"approval_id": decision.token.token_id if decision.token else None,
              "approval_status": decision.status.value, "requires_user_confirmation": True,
              "business_write_performed": False, "preview": preview.model_dump(mode="json")},
        evidence=[EvidenceSource(source="browser_observation", source_ref=request.application_id)])
