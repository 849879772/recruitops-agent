"""Read-only semantic labels; a legacy placeholder zero is not a probability."""

from .binding import BINDING_KEY


def mail_semantics(record):
    metadata = record.raw_metadata or {}
    analysis = metadata.get("model_analysis", {})
    valid_analysis = bool(analysis and analysis.get("digest") == record.content_digest)
    proposal = analysis.get("payload", {}) if valid_analysis else {}
    event = proposal.get("event_type")
    state = record.processing_status
    failed = state in {"failed", "failed_terminal"}
    analysis_state = "failed" if failed else "analyzed" if valid_analysis else "unassessed"
    no_association = state in {"irrelevant", "ignored"} or (valid_analysis and (
        event in {"information", "action_required", "application_confirmation"}
        or (event == "assessment" and not proposal.get("job_title") and not proposal.get("job_code"))))
    binding = metadata.get(BINDING_KEY, {})
    if binding and (binding.get("state") != "bound" or binding.get("content_digest") != record.content_digest):
        binding_state = "unbound" if binding.get("state") == "unbound" else "stale"
    elif record.application_id:
        binding_state = "confirmed" if binding else "linked"
    else:
        binding_state = "not_required" if no_association else "pending" if valid_analysis else "unassessed"
    status_labels = {
        "pending": "待分析", "processed_updated": "已更新投递阶段", "processed_unchanged": "已核对，状态未变化",
        "processed": "已处理", "irrelevant": "非招聘邮件", "pending_association": "待关联",
        "ambiguous_application": "待确认投递", "needs_auth_metadata": "发件人待核验",
        "failed_terminal": "分析或核验失败", "failed": "处理失败", "ignored": "已忽略",
    }
    return {"analysis_state": analysis_state, "binding_state": binding_state,
            "association_required": not no_association, "event_type": event,
            "processing_label": status_labels.get(state, "待确认"),
            "confidence": None, "legacy_confidence": record.confidence,
            "confidence_kind": "not_evaluated", "binding_revision": int(binding.get("revision", 0)),
            "content_digest": record.content_digest}
