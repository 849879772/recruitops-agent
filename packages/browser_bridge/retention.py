"""Twelve-hour diagnostics, latest-only review receipts, durable business history."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from time import perf_counter

from sqlalchemy import delete, or_, select, text

from packages.storage.application_reviews import IDENTITY_REASONS, review_time, save_latest_reviews
from packages.storage.models import (
    Approval, ApplicationSnapshot, BrowserOperation, BrowserOperationEvent,
    BrowserOutbox, TaskRun, ToolCall,
)

DETAIL_HOURS = 12
MARKER = 'diagnostics_compacted_v1'


def _refs(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'operation_id', 'observation_operation_id', 'vision_operation_id', 'source_ref'} and isinstance(item, str):
                yield item
            else:
                yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def _pins(session, now):
    pins = set()
    for approval in session.scalars(select(Approval).where(Approval.status.in_(['pending', 'approved'])).execution_options(yield_per=100)):
        preview = approval.preview or {}
        expiry = preview.get('expires_at')
        # Approved/expired receipts do not pin full pages indefinitely.
        if expiry and review_time(expiry, now) <= now:
            continue
        if approval.status == 'pending' or expiry:
            pins.update(_refs(preview))
    for call in session.scalars(select(ToolCall).where(ToolCall.tool_name == 'application_review_checkpoint').execution_options(yield_per=100)):
        state = call.arguments or {}
        # Unknown legacy states and recoverable tasks must keep their checkpoints.
        if state.get('run_status') not in {'completed', 'cancelled'}:
            pins.update(_refs(state))
    return pins - {None, ''}


def _summary(value, when):
    if isinstance(value, dict) and value.get(MARKER):
        # Do not retain candidate payloads after identity confirmation is settled.
        return {key: value[key] for key in (MARKER, 'compacted_at', 'original_bytes', 'sha256') if key in value}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
    summary = {MARKER: True, 'compacted_at': when.isoformat(), 'original_bytes': len(raw),
               'sha256': sha256(raw).hexdigest()}
    if isinstance(value, dict):
        for key in ('code', 'model', 'usage', 'image_sha256', 'image_count', 'provider_request_attempted',
                    'attempt', 'format_repair', 'reading_version', 'error_code', 'captured_at'):
            if key in value and len(json.dumps(value[key], default=str)) <= 1000:
                summary[key] = value[key]
    return summary


def _identity_only(value, *, verified_vision=False):
    """Enough to confirm card identity, never evidence for a stage write."""
    result = {key: value[key] for key in ('page_url', 'navigation_binding') if key in value}
    result['page'] = {'page_url': (value.get('page') or {}).get('page_url')}
    from packages.tools.application_page_evidence import identity_fallback_cards
    cards = value.get('application_records', []) or identity_fallback_cards(value, verified_vision=verified_vision)
    result['application_records'] = [{key: item[key] for key in (
        'title', 'raw_title', 'application_id', 'job_id', 'evidence_source',
    ) if key in item} for item in cards if isinstance(item, dict)]
    return result


def _checkpoints(session, now, limit, dry_run, result):
    from packages.tools.application_review_run import _response
    query = select(ToolCall).where(
        ToolCall.tool_name == 'application_review_checkpoint',
        ToolCall.arguments['details_expired'].as_boolean().is_not(True),
        ToolCall.arguments['run_status'].as_string().in_(['completed', 'cancelled']),
        or_(ToolCall.arguments['latest_reviews_saved'].as_boolean().is_not(True),
            ToolCall.updated_at < now - timedelta(hours=DETAIL_HOURS)),
    ).order_by(ToolCall.updated_at.desc()).limit(limit)
    if session.bind.dialect.name == 'postgresql':
        query = query.with_for_update(skip_locked=True)
    for call in session.scalars(query):
        if dry_run:
            continue
        state = dict(call.arguments or {})
        when = review_time(call.updated_at, now)
        rows = [{**row, 'application_id': app_id} for app_id, row in (state.get('results') or {}).items()
                if isinstance(row, dict) and app_id in state.get('ids', [])]
        save_latest_reviews(session, rows, checked_at=when, run_id=call.task_id)
        state['latest_reviews_saved'] = True
        if when < now - timedelta(hours=DETAIL_HOURS):
            # Small terminal receipt, without per-company rows or page payloads.
            defaults = {'database_total': len(state.get('ids', [])), 'excluded_terminal': 0, 'pages_total': 0}
            summary = _response(call.task_id, {**defaults, **state}, perf_counter()).summary
            summary.pop('identity_confirmation_items', None)
            summary.pop('next_action', None)
            state = {'details_expired': True, 'run_status': state['run_status'],
                     'metadata': state.get('metadata', {}), 'summary': summary,
                     'compacted_at': now.isoformat()}
            result['checkpoints_compacted'] += 1
        call.arguments = state
        call.updated_at = when  # Maintenance is not a fresh check.
    session.flush()


def compact_browser_diagnostics(storage, *, enabled=False, now=None, batch_size=100, dry_run=True):
    """Explicit opt-in, bounded transaction, no file deletion or database rewrite."""
    result = {'enabled': enabled, 'dry_run': dry_run, 'selected': 0, 'compacted': 0,
              'events_removed': 0, 'outbox_removed': 0, 'checkpoints_compacted': 0, 'skipped_active': False}
    if not enabled:
        return result
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('retention time must include timezone')
    limit = max(1, min(batch_size, 100))
    with storage.transaction(write=not dry_run) as session:
        if storage.engine.dialect.name == 'postgresql':
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            if not session.scalar(text('SELECT pg_try_advisory_xact_lock(718202609)')):
                result['skipped_active'] = True
                return result
        if session.scalar(select(TaskRun.id).where(TaskRun.status == 'running').limit(1)):
            result['skipped_active'] = True
            return result
        _checkpoints(session, now, limit, dry_run, result)
        pins = _pins(session, now)
        identity_ops = set()
        for receipt in session.scalars(select(ApplicationSnapshot.last_review).where(ApplicationSnapshot.last_review.is_not(None))):
            if receipt and receipt.get('state') == 'unresolved' and receipt.get('reason') in IDENTITY_REASONS:
                if receipt.get('operation_id'):
                    identity_ops.add(receipt['operation_id'])
        level = BrowserOperation.result['retention_level'].as_string()
        eligible_level = or_(level.is_(None), level != 'events')
        if identity_ops:
            eligible_level = eligible_level & ~((level == 'identity') & BrowserOperation.operation_id.in_(identity_ops))
            # SQL NULL must remain eligible.
            eligible_level = or_(level.is_(None), eligible_level)
        query = select(BrowserOperation).where(
            BrowserOperation.operation == 'observe_application_status_page',
            BrowserOperation.status.in_(['SUCCEEDED', 'STATE_UNCLEAR', 'FAILED', 'CANCELLED']),
            BrowserOperation.completed_at < now - timedelta(hours=DETAIL_HOURS),
            BrowserOperation.updated_at < now - timedelta(hours=DETAIL_HOURS),
            eligible_level,
            ~select(BrowserOutbox.outbox_id).where(
                BrowserOutbox.operation_id == BrowserOperation.operation_id,
                BrowserOutbox.acked_at.is_(None)).exists(),
        ).order_by(BrowserOperation.updated_at, BrowserOperation.operation_id).limit(limit)
        if pins:
            query = query.where(BrowserOperation.operation_id.notin_(pins))
        if storage.engine.dialect.name == 'postgresql':
            query = query.with_for_update(skip_locked=True)
        for operation in session.scalars(query):
            result['selected'] += 1
            if dry_run:
                continue
            events = list(session.scalars(select(BrowserOperationEvent).where(
                BrowserOperationEvent.operation_id == operation.operation_id)))
            keep_identity = operation.operation_id in identity_ops
            value = operation.result or {}
            operation.result = {**_summary(value, now),
                                **(_identity_only(value, verified_vision=any(
                                    e.event_type == 'vision_analysis' and e.payload == value.get('vision')
                                    for e in events)) if keep_identity else {}),
                                'retention_level': 'identity' if keep_identity else 'events'}
            operation.updated_at = now
            previous = next((e.payload for e in events if e.event_type == 'retention_summary'), None)
            types = dict(Counter(e.event_type for e in events)) if previous is None else previous.get('event_counts', {})
            session.execute(delete(BrowserOperationEvent).where(BrowserOperationEvent.operation_id == operation.operation_id))
            sequence = max([operation.last_event_sequence, *(e.sequence for e in events)]) + 1
            session.add(BrowserOperationEvent(event_id='retention-' + operation.operation_id,
                operation_id=operation.operation_id, sequence=sequence, status=operation.status,
                event_type='retention_summary', payload={MARKER: True, 'event_counts': types}, occurred_at=now))
            operation.last_event_sequence = sequence
            result['events_removed'] += max(0, len(events) - 1)
            removed = session.execute(delete(BrowserOutbox).where(BrowserOutbox.operation_id == operation.operation_id))
            result['outbox_removed'] += removed.rowcount
            result['compacted'] += 1
    return result
