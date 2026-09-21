"""Bounded model mail processing shared by conversational tools and schedules."""

import json
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4
from hashlib import sha256
from dataclasses import replace

from sqlalchemy import select
from pydantic import ValidationError

from packages.matching.client import DeepSeekClient
from .analysis_store import save_model_analysis, get_model_analysis, sync_analysis_labels
from .analysis_binding import parsed_model_evidence, model_application_matches
from .model_analysis import (
    MAIL_ANALYSIS_VERSION, MailTriageProposal, MailAnalysisProposal,
    validate_mail_proposal,
)
from .model_prompts import (build_batch_triage_prompt, build_full_analysis_prompt,
                            TRIAGE_OUTPUT_SCHEMA, FULL_ANALYSIS_OUTPUT_SCHEMA)
from .storage import RecruitmentMailRecord
from .record_view import parsed_record
from .models import ParsedRecruitmentEmail


DONE = {"processed_updated", "processed_unchanged", "processed", "irrelevant", "ignored"}
TARGETS = {"application_confirmation": "applied", "assessment": "applied",
           "written_test": "written", "interview": "interview1", "offer": "offer", "rejection": "rejected"}


def _decode_model_json(content):
    text = content.strip()
    lines = text.splitlines()
    if len(lines) >= 3 and lines[0].lower() in {"```json", "```"} and lines[-1] == "```":
        text = "\n".join(lines[1:-1])
    return json.loads(text)


def _analysis_proposal(content, spans=None):
    payload = _decode_model_json(content)
    if isinstance(payload, dict):
        if spans is not None:
            ids = payload.get("evidence_ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(not isinstance(i, str) or i not in spans for i in ids):
                raise ValueError("invalid_evidence_ids")
            payload = dict(payload, evidence_quotes=[spans[i] for i in dict.fromkeys(ids)])
        # Extra provider metadata has no authority: project onto the allowed evidence fields.
        payload = {key: value for key, value in payload.items() if key in MailAnalysisProposal.model_fields}
    return MailAnalysisProposal.model_validate(payload)


def _failure_diagnostic(exc):
    if isinstance(exc, ValidationError):
        # Never persist model values or raw validation messages containing mail content.
        local_cache = exc.title == "ParsedRecruitmentEmail"
        known = set(MailAnalysisProposal.model_fields) | set(MailTriageProposal.model_fields) | set(ParsedRecruitmentEmail.model_fields)
        return {"kind": "stored_mail_schema" if local_cache else "schema", "fields": [
            {"field": str(e["loc"][0]) if e["loc"] and e["loc"][0] in known else "unknown",
             "type": e["type"]}
            for e in exc.errors(include_input=False, include_context=False, include_url=False)[:10]]}
    if isinstance(exc, json.JSONDecodeError):
        return {"kind": "json", "position": exc.pos, "length": len(exc.doc)}
    if isinstance(exc, ValueError) and type(exc).__name__ == "MailAnalysisValidationError":
        return {"kind": "provenance", "code": exc.code}
    if isinstance(exc, ValueError) and str(exc) in {"company_not_in_source", "job_not_in_source", "job_code_not_in_source", "missing_event_evidence"}:
        return {"kind": "provenance", "code": str(exc)}
    if isinstance(exc, ValueError) and str(exc) in {"mail_body_exceeds_bounds", "processing_claim_lost", "invalid_evidence_ids", "triage_records_mismatch"}:
        return {"kind": "validation", "code": str(exc)}
    return {"kind": type(exc).__name__}


def _attempt_current(record):
    attempt = (record.raw_metadata or {}).get("model_processing", {})
    return attempt if attempt.get("digest") == record.content_digest and attempt.get("version") == MAIL_ANALYSIS_VERSION else None


def _input_digest(record, applications):
    from .identity import normalize_company_name, _CONTROLLED_COMPANY_ALIAS_GROUPS
    body = normalize_company_name(f"{record.sender}\n{record.subject}\n{record.body_text}")
    # Invalidation depends on source/candidates, never on the proposal produced by this attempt.
    relevant = [a for a in applications if (
        normalize_company_name(a.company_name) in body or any(
            normalize_company_name(a.company_name) in group and any(alias in body for alias in group)
            for group in _CONTROLLED_COMPANY_ALIAS_GROUPS)
    )]
    values = {"applications": sorted(
        [(a.id, a.company_name, a.job_title, a.stage.value, str(a.source_status_synced_at)) for a in relevant]),
        "transport": (record.raw_metadata or {}).get("transport", {})}
    return sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _eligible(record, input_digest=None):
    if record.processing_status in DONE:
        return False
    attempt = _attempt_current(record)
    if not attempt:
        return True
    if input_digest is not None and attempt.get("state") != "running" and attempt.get("inputs") != input_digest:
        return True
    if attempt.get("state") != "running":
        return False
    started = datetime.fromisoformat(attempt["started_at"])
    return (datetime.now(timezone.utc) - started).total_seconds() > 180


def _claim(store, record, owner, input_digest):
    with store.storage.write_transaction() as session:
        current = session.scalar(select(RecruitmentMailRecord).where(
            RecruitmentMailRecord.id == record.id).with_for_update())
        if current is None or current.content_digest != record.content_digest or not _eligible(current, input_digest):
            return False
        metadata = dict(current.raw_metadata)
        metadata["model_processing"] = {
            "digest": current.content_digest, "version": MAIL_ANALYSIS_VERSION,
            "owner": owner, "state": "running", "inputs": input_digest,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        current.raw_metadata = metadata
    return True


def _finish(store, record, owner, state, reason=None, inputs=None, diagnostic=None):
    with store.storage.write_transaction() as session:
        current = session.scalar(select(RecruitmentMailRecord).where(
            RecruitmentMailRecord.id == record.id).with_for_update())
        if current is None or current.content_digest != record.content_digest:
            raise ValueError("mail_changed_during_processing")
        metadata = dict(current.raw_metadata)
        attempt = dict(metadata.get("model_processing", {}))
        if attempt.get("owner") != owner:
            raise ValueError("processing_claim_lost")
        attempt.update(state=state, reason=reason)
        if diagnostic is not None:
            attempt["diagnostic"] = diagnostic
        if inputs is not None:
            attempt["inputs"] = inputs
        metadata["model_processing"] = attempt
        current.raw_metadata = metadata
        current.processing_status = state
        current.processing_error = reason
        current.processed_at = datetime.now(timezone.utc)


def _mail_input(record):
    return {"record_id": record.id, "content_digest": record.content_digest,
            "subject": record.subject, "sender": record.sender,
            "body_text": record.body_text, "received_at": record.received_at}


def _model_call(client, bundle, schema):
    structured = getattr(client, "complete_structured", None)
    kwargs = dict(system_prompt=bundle.system_prompt, user_prompt=bundle.user_prompt, max_tokens=4000)
    return structured(schema=schema, **kwargs) if callable(structured) else client.complete(**kwargs)


def processing_status(store, *, limit=50, include_history=False):
    scope = _mail_scope(store, None)
    records = [r for r in scope if include_history or r.processing_status not in DONE][:min(limit, 200)]
    return {"observed_at": datetime.now(timezone.utc).isoformat(),
            "unfinished_count": sum(r.processing_status not in DONE for r in scope),
            "include_history": include_history,
            "items_truncated": sum(include_history or r.processing_status not in DONE for r in scope) > len(records),
            "items": [{"record_id": r.id, "subject": r.subject,
                        "processing_status": r.processing_status,
                        "eligible": _eligible(r), "reason": r.processing_error,
                        "diagnostic": (r.raw_metadata or {}).get("model_processing", {}).get("diagnostic"),
                        "action_summary": ((get_model_analysis(store, r.id) or {}).get("payload") or {}).get("action_summary")}
                       for r in records]}


def _mail_scope(store, record_ids):
    if record_ids is not None:
        records = [r for ident in dict.fromkeys(record_ids)
                   if (r := store.get(record_id=ident)) is not None]
    else:
        records, offset = [], 0
        while True:
            page = store.query(limit=200, offset=offset)
            records.extend(page)
            if len(page) < 200:
                break
            offset += 200
    return records


def _remaining_mail(store, applications, record_ids):
    return [r for r in _mail_scope(store, record_ids)
            if _eligible(r, _input_digest(r, applications))]


def _batch_outcome(summary, store, repository, record_ids):
    items = [r["schedule_item"] for r in summary["results"] if r.get("schedule_item")]
    summary["schedule_items_created"] = sum(bool(item["created"]) for item in items)
    summary["schedule_items_time_unconfirmed"] = sum(item["time_kind"] == "unspecified" for item in items)
    scope = _mail_scope(store, record_ids)
    applications = repository.list_applications()
    remaining = [r for r in scope if _eligible(r, _input_digest(r, applications))]
    unfinished = sum(r.processing_status not in DONE for r in scope)
    summary.update(remaining_count=len(remaining), has_more=bool(remaining),
                   unfinished_count=unfinished,
                   next_record_ids=[r.id for r in remaining[:50]])
    summary["scope_complete"] = not unfinished and not summary["failed"] and not summary["unresolved"]
    if not summary["scope_complete"]:
        summary["status"] = "partial"
    return summary


def process_pending_mail(store, repository, settings, *, limit=20, record_ids=None, client=None):
    summary = {"status": "completed", "processed": 0, "updated": 0, "unchanged": 0,
               "irrelevant": 0, "unresolved": 0, "failed": 0, "notifications": 0, "reminders": 0, "results": []}
    if not settings.write_enabled:
        return dict(summary, status="blocked", reason="write_disabled")
    if client is None:
        if not settings.llm_enabled or not settings.llm_api_key:
            return dict(summary, status="blocked", reason="mail_model_unavailable")
        client = DeepSeekClient(api_key=settings.llm_api_key, model=settings.llm_model,
                                endpoint=settings.llm_endpoint, max_tokens=4000,
                                api_style=settings.model_api_style,
                                timeout=min(settings.llm_timeout_seconds, 25), max_attempts=1)
    limit = max(1, min(limit, 50))
    applications = repository.list_applications()
    records = _remaining_mail(store, applications, record_ids)[:limit]
    if not records:
        return _batch_outcome(summary, store, repository, record_ids)
    started, owner = monotonic(), uuid4().hex
    summary["has_more"] = len(records) > 10
    if summary["has_more"]:
        summary["status"] = "partial"
    # Claim only a small batch so an interrupted call cannot monopolize the mailbox.
    records = [r for r in records[:10] if _claim(store, r, owner, _input_digest(r, applications))]
    if not records:
        return _batch_outcome(summary, store, repository, record_ids)
    bundle = build_batch_triage_prompt([_mail_input(r) for r in records])
    try:
        raw = _model_call(client, bundle, TRIAGE_OUTPUT_SCHEMA)
        proposals = [MailTriageProposal.model_validate(x) for x in _decode_model_json(raw.content)]
        if len(proposals) != len(records) or {p.record_id for p in proposals} != {r.id for r in records}:
            raise ValueError("triage_records_mismatch")
        triage = {p.record_id: p for p in proposals}
        for record in records:
            validate_mail_proposal(triage[record.id], record)
    except Exception as exc:
        diagnostic = _failure_diagnostic(exc)
        for record in records:
            _finish(store, record, owner, "failed_terminal", "triage_" + type(exc).__name__, diagnostic=diagnostic)
        return _batch_outcome(dict(summary, status="partial", failed=len(records),
                                   processed=len(records), reason="triage_failed"),
                              store, repository, record_ids)
    for record in records:
        if monotonic() - started > 90:
            _finish(store, record, owner, "pending", None)
            # Pending time-budget results should be eligible on the next bounded call.
            with store.storage.write_transaction() as session:
                row = session.get(RecruitmentMailRecord, record.id)
                metadata = dict(row.raw_metadata)
                metadata.pop("model_processing", None)
                row.raw_metadata = metadata
            summary["status"] = "partial"
            summary["has_more"] = True
            continue
        try:
            proposal = triage[record.id]
            if proposal.relevance.value == "irrelevant":
                save_model_analysis(store, record.id, record.content_digest, MAIL_ANALYSIS_VERSION,
                                    proposal.model_dump(mode="json"), "irrelevant", getattr(client, "model", None))
                sync_analysis_labels(store, record.id)
                _finish(store, record, owner, "irrelevant")
                summary["irrelevant"] += 1
                result = {"record_id": record.id, "state": "irrelevant", "reason": proposal.reason}
            else:
                result = _analyze_one(store, repository, settings, client, record, applications, owner)
                bucket = result.get("summary_bucket") or {"processed_updated": "updated", "processed_unchanged": "unchanged",
                          "processed": "notifications"}.get(result["state"], "unresolved")
                summary[bucket] += 1
            summary["results"].append(result)
        except Exception as exc:
            diagnostic = _failure_diagnostic(exc)
            reason = "analysis_" + diagnostic.get("code", type(exc).__name__)
            _finish(store, record, owner, "failed_terminal", reason, diagnostic=diagnostic)
            summary["failed"] += 1
            summary["results"].append({"record_id": record.id, "state": "failed_terminal",
                                       "reason": reason, "diagnostic": diagnostic})
        summary["processed"] += 1
    if summary["failed"] or summary["unresolved"]:
        summary["status"] = "partial"
    return _batch_outcome(summary, store, repository, record_ids)


def _analyze_one(store, repository, settings, client, record, applications, owner):
    from packages.tools.application_status_update import ApplicationStatusUpdateInput, update_application_status
    from .association import find_stale_company_only_match
    def check_claim():
        current = store.get(record_id=record.id)
        attempt = (current.raw_metadata or {}).get("model_processing", {}) if current else {}
        if (current is None or current.content_digest != record.content_digest
                or attempt.get("owner") != owner or attempt.get("state") != "running"):
            raise ValueError("processing_claim_lost")

    check_claim()
    # A malformed local cache cannot be corrected by spending another model call.
    parsed_record(record)
    if len(record.body_text) > 20000:
        raise ValueError("mail_body_exceeds_bounds")
    # Interpret the source independently; verify identity against ALL applications below.
    # The size of the user's application history must not bound email interpretation.
    bundle = build_full_analysis_prompt(_mail_input(record), [])
    spans = None
    schema = FULL_ANALYSIS_OUTPUT_SCHEMA
    if callable(getattr(client, "complete_structured", None)):
        spans = {"subject": record.subject}
        for start in range(0, len(record.body_text), 350):
            value = record.body_text[start:start + 400]
            if value.strip():
                spans[f"body_{start}"] = value
        if not spans["subject"].strip():
            spans.pop("subject")
        schema = dict(FULL_ANALYSIS_OUTPUT_SCHEMA, properties=dict(FULL_ANALYSIS_OUTPUT_SCHEMA["properties"]))
        schema["properties"]["evidence_ids"] = {"type": "array", "items": {"type": "string", "enum": list(spans)}, "minItems": 1, "maxItems": 20}
        schema["required"] = [*schema["required"], "evidence_ids"]
        bundle = replace(bundle, user_prompt=bundle.user_prompt +
            "\nSelect evidence_ids from the following untrusted source spans. Do not rewrite quotations. "
            "Return evidence_quotes as []; the program copies the selected spans verbatim. "
            "Select spans that explicitly support identity and event, not merely generic process descriptions.\n" +
            json.dumps(spans, ensure_ascii=True))
    for attempt in range(2):
        response = _model_call(client, bundle, schema)
        check_claim()
        try:
            proposal = _analysis_proposal(response.content, spans)
            validate_mail_proposal(proposal, record)
            parsed = parsed_model_evidence(record, proposal.model_dump(mode="json"))
            break
        except ValueError as exc:
            if attempt:
                raise
            # One source-grounded correction before any write; never retry rejected writes.
            bundle = replace(bundle, user_prompt=bundle.user_prompt +
                "\nOne correction is allowed. Validation diagnostic: " + json.dumps(_failure_diagnostic(exc)) +
                "\nRead the same original source again. Copy short contiguous quotations exactly; "
                "do not join separated spans, paraphrase, or copy company/job text from a candidate. "
                "Use null for facts absent from the mail. Preserve the supplied record ID and digest.")
    save_model_analysis(store, record.id, record.content_digest, MAIL_ANALYSIS_VERSION + ":" + _input_digest(record, applications),
                        proposal.model_dump(mode="json"), "proposed", getattr(response, "model", None) or getattr(client, "model", None))
    sync_analysis_labels(store, record.id)
    event = proposal.event_type.value
    from .scheduling import ensure_mail_schedule
    matches = [a for a in applications if model_application_matches(record, proposal.model_dump(mode="json"), a)]
    schedule_application = matches[0] if len(matches) == 1 and (proposal.job_title or proposal.job_code) and (
        not proposal.candidate_application_id or proposal.candidate_application_id == matches[0].id) else None
    schedule_item = ensure_mail_schedule(store, record, proposal, owner, schedule_application)
    if event in {"information", "action_required", "application_confirmation"}:
        _finish(store, record, owner, "processed")
        return {"record_id": record.id, "state": "processed", "event_type": event,
                "summary_bucket": "reminders" if event == "action_required" else "notifications",
                "association_required": False,
                "action_summary": proposal.action_summary, "deadline": proposal.deadline,
                "schedule_item": schedule_item}
    if event == "assessment" and not proposal.job_title and not proposal.job_code:
        _finish(store, record, owner, "processed")
        return {"record_id": record.id, "state": "processed", "event_type": event,
                "summary_bucket": "reminders", "association_required": False,
                "company_name": proposal.company_name, "reason": "company_assessment_reminder",
                "action_summary": proposal.action_summary or "查看邮件并完成测评",
                "deadline": proposal.deadline, "schedule_item": schedule_item}
    stale = find_stale_company_only_match(parsed, applications)
    if not matches and stale:
        matches = [stale]
    if len(matches) != 1 or (proposal.candidate_application_id and proposal.candidate_application_id != matches[0].id):
        reason = "multiple_candidates" if len(matches) > 1 else "no_verified_match"
        _finish(store, record, owner, "ambiguous_application", reason)
        return {"record_id": record.id, "state": "ambiguous_application", "reason": reason,
                "candidate_application_id": proposal.candidate_application_id, "schedule_item": schedule_item}
    if event not in TARGETS:
        _finish(store, record, owner, "pending_association", "event_unknown")
        return {"record_id": record.id, "state": "pending_association", "reason": "event_unknown"}
    check_claim()
    outcome = update_application_status(ApplicationStatusUpdateInput(
        application_id=matches[0].id, evidence_type="mail", evidence_id=record.id,
        target_status=TARGETS[event]), repository, store, settings=settings)
    latest = store.get(record_id=record.id)
    if not outcome.success and latest.processing_status in {"pending", "linked"}:
        store.update_processing_status(record.id, "failed_terminal", processing_error="status_validation_failed")
        latest = store.get(record_id=record.id)
    _finish(store, record, owner, latest.processing_status, latest.processing_error,
            inputs=_input_digest(latest, repository.list_applications()))
    return {"record_id": record.id, "state": latest.processing_status,
            "application_id": matches[0].id, "write_result": outcome.model_dump(mode="json"),
            "schedule_item": schedule_item}
