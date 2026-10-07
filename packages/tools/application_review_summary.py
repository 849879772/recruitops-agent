"""Complete projections of final review rows; retries are not separate results."""

from collections import Counter
from packages.storage.application_reviews import IDENTITY_REASONS


IDENTITY_CONFIRMATION_REASONS = IDENTITY_REASONS
# Presentation only. Missing evidence never verifies a stage or authorizes a write.
# Access, transport, model faults and conflicting evidence remain actionable.
RETAINED_REASONS = (IDENTITY_CONFIRMATION_REASONS - {"model_identity_mismatch"}) | frozenset({
    "record_present_status_unknown", "unparsed_page", "application_records_missing",
    "status_unmapped", "model_uncertain", "talent_pool_status_unmapped",
    "position_recommendation_unmapped",
})


def review_result_presentation(row):
    visual = getattr(row, "vision_disposition", None)
    visual_fault = visual not in {
        None, "not_requested", "not_configured", "analyzed", "cache_hit", "skipped",
        "disabled_by_request", "disabled_in_settings", "skipped_blank_page", "skipped_frame_restricted", "skipped_task_stopped",
    }
    retained = row.state == "unresolved" and not row.wrote and row.reason in RETAINED_REASONS and not visual_fault
    # Recompute, including old checkpoints, rather than trusting a saved UI flag.
    return row.model_copy(update={"presentation_state": "retained" if retained else None})


def review_presentation_summary(rows, *, visual_operations=None):
    retained = [row for row in rows if review_result_presentation(row).presentation_state == "retained"]
    confirmations = [row for row in rows if row.state == "unresolved" and row.reason in IDENTITY_CONFIRMATION_REASONS]
    # One image request can serve several jobs. Never count copied per-row audit
    # fields as separate provider requests or imply screenshots were taken on opt-out.
    visual_operations = dict(visual_operations or {})
    for row in rows:
        audit = getattr(row, "diagnostics", None) or {}
        if audit.get("vision_operation_id"):
            visual_operations[audit["vision_operation_id"]] = audit
    return {
        "retained_count": len(retained),
        "unchanged_or_retained_count": sum(row.state == "unchanged" for row in rows) + len(retained),
        "retained_by_stage": dict(sorted(Counter(row.saved_stage or "unknown" for row in retained).items())),
        "attention_required_count": sum(row.state == "unresolved" for row in rows) - len(retained),
        # A subset, not extra results. No authority to select or approve a card.
        "identity_confirmation_items": [{
            "application_id": row.application_id, "company_name": row.company_name,
            "job_title": row.job_title, "reason": row.reason, "operation_id": row.operation_id,
        } for row in confirmations],
        "presentation_note": "未发现可确认的新进展，保留原阶段；不代表官网已核验无变化。",
        "model_record_dispositions": dict(Counter(getattr(row, "model_disposition", None) or "not_requested" for row in rows)),
        "vision_record_dispositions": dict(Counter(getattr(row, "vision_disposition", None) or "not_requested" for row in rows)),
        "vision_provider_request_count": sum(audit.get("vision_provider_request_count", 0) for audit in visual_operations.values()),
        "vision_analysis_count": sum(audit.get("vision_analysis_count", 0) for audit in visual_operations.values()),
        "vision_image_count": sum(audit.get("vision_image_count", 0) for audit in visual_operations.values()),
    }


def review_reason_breakdown(rows):
    buckets = {state: Counter() for state in (
        "updated", "unchanged", "excluded", "blocked", "unresolved", "failed",
    )}
    for row in rows:
        buckets[row.state][row.reason or "reason_not_reported"] += 1
    return {state: dict(sorted(counts.items())) for state, counts in buckets.items()}
