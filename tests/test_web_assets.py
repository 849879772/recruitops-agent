from pathlib import Path


WEB_ROOT = Path(__file__).parents[1] / "apps" / "web"


def test_latest_ui_assets_and_manual_api_contract():
    import re
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    configuration = (WEB_ROOT / "configuration.js").read_text(encoding="utf-8")
    for asset in re.findall(r'(?:src|href)="\./([^"?]+)', html):
        assert (WEB_ROOT / asset).is_file(), asset
    assert 'id="application-add-button"' in html
    assert 'id="application-create-dialog"' in html
    assert '/api/local-ui/applications/manual' in app
    assert 'body.record_url ||= null' in app
    assert 'localizeCodexRuntimeMessage(record.body)' in app
    assert 'await loadApplications();' in app
    assert 'returnToJobBrowseLocation()' in app
    assert 'model_connections: connections' in configuration
    assert 'post("model/test",' in configuration
    assert 'post("mail/test",' in configuration
    assert '配置 API 尚未支持' in configuration
    assert 'write_enabled' not in configuration
    for field in ("mail_enabled", "job_analysis_enabled", "automation_enabled"):
        assert f'name="{field}"' not in html
    assert 'name="mail_sync_on_startup"' not in html
    assert 'name="vision_enabled"' in html
    assert "配置成功后，启动时会自动同步" in html
    assert "不配置不会影响助理、岗位抓取、评分、投递记录和日程" in html
    for private_marker in ('8012', '5433', 'D:/RecruitOps-Agent', '周帅康'):
        assert private_marker not in html + app + configuration


def test_dashboard_assets_are_local_and_reference_api() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
    assert "./styles.css" in html
    assert "./app.js" in html
    assert "https://" not in html + js + css
    for legacy in (
        "/api/assistant",
        "/api/agent/",
        "/api/browser/application-status/reviews",
        "review-progress",
        "等待 Edge 扩展领取",
        "等待浏览器扩展",
        "领取",
    ):
        assert legacy not in html + js + css
    assert "/api/codex/health" in js
    assert "/api/codex/threads" in js
    assert "/api/codex/traces" in js
    assert "approvalForTask" in js
    assert 'authHeaders("登记审批预览"' in js
    assert 'authHeaders("确认审批决定")' in js
    assert '"X-RecruitOps-Local-UI": "1"' in js
    assert "requestSessionToken" not in js
    assert 'approval.operation === "browser_action"' in js
    assert "打开待复核页面" in js
    assert "复制单次令牌" in js
    assert "copyApprovalToken" in js
    assert "/api/approvals" in js
    assert "textContent" in js


def test_dashboard_has_required_views_and_accessibility_labels() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    for view in (
        "dashboard",
        "assistant",
        "jobs",
        "companies",
        "applications",
        "schedule",
        "automations",
        "mail",
        "tasks",
        "approvals",
        "integrations",
    ):
        assert f'data-view-panel="{view}"' in html
    assert 'aria-label="主导航"' in html
    assert 'aria-live="polite"' in html


def test_jobs_view_keeps_legacy_browsing_features_without_previous_cohort() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    for control in (
        "job-search",
        "job-company-filter",
        "job-category-filter",
        "job-platform-filter",
        "job-evaluation-filter",
        "job-score-filter",
        "job-sort",
        "jobs-prev-button",
        "jobs-next-button",
        "featured-job-list",
        "job-detail-dialog",
    ):
        assert f'id="{control}"' in html
    assert "/api/jobs/browse" in js
    assert "renderFeaturedJobs" in js
    assert "renderJobDetail" in js
    assert "往届" not in html
    assert "届别待确认" not in html


def test_company_ranking_is_not_overwritten_by_today_job_facets() -> None:
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "companySummaries: []" in js
    assert 'if (state.jobBrowse.mode === "all") renderJobFacets(payload.facets);' in js
    assert "state.companySummaries.filter" in js
    assert 'api("/api/jobs/browse?limit=1&offset=0&sort=score")' in js


def test_jobs_view_keeps_unscored_filters_but_never_offers_excluded_jobs() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    for state in ("pending", "jd_incomplete", "unscored"):
        assert f'option value="{state}"' in html
    assert 'option value="excluded"' not in html
    assert 'setText("jobs-unscored-total", stats.pending ?? 0)' in js
    assert 'scoreBadge(job.match_score, job.analysis_status)' in js
    assert 'status && status !== "complete"' in js


def test_primary_navigation_is_user_facing_and_system_tools_are_collapsed() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'class="system-nav"' in html
    assert '>数据接入<' in html
    assert '>运行记录<' in html
    assert ">主项目报告<" not in html
    for label in ("27届校招", "今日新增", "公司排行", "投递记录", "日程安排", "定时任务", "招聘邮箱", "求职助理"):
        assert f">{label}<" in html


def test_jobs_view_explains_runtime_data_provenance() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'class="source-note"' in html
    assert "RecruitOps 本地 PostgreSQL" in html
    assert "公司发现来自 OfferBiu" in html
    assert "不会实时读取原秋招系统" in html


def test_autumn_navigation_controls_job_mode_and_assistant_handoffs() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'data-job-nav-mode="all"' in html
    assert 'data-job-nav-mode="today"' not in html
    assert 'id="nav-today-count"' not in html
    assert 'data-job-mode="all"' in html
    assert 'data-job-mode="today"' in html
    assert "renderJobModePresentation" in js
    assert 'switchView("assistant");\n        openAssistantDraft' in js


def test_company_view_does_not_expose_retired_oc_adapter_queue() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="adapter-queue"' not in html
    assert "/api/oc/" not in js
    assert "renderAdapterCandidates" not in js


def test_assistant_uses_natural_language_intents_and_renders_messages() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert '<textarea class="assistant-message-input"' in html
    assert 'id="assistant-messages"' in html
    assert 'id="assistant-intent"' in html
    assert "INTENT_RULES" in js
    assert "mapIntent" in js
    assert "tool_response" in js
    assert "grounded_evidence" in js
    assert "response.evidence" in js
    assert "失败或停止原因" in js
    assert "compactResultValue" in js
    assert "查看结构化结果" in js
    assert "display_label" in js


def test_assistant_quick_actions_are_user_facing_and_explicit() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    quick_questions = html.split('id="quick-questions"', 1)[1].split("</div>", 1)[0]
    assert quick_questions.count('class="command-button"') == 5
    assert 'data-task="full_recruitment_sync"' in quick_questions
    assert "全量爬取" in quick_questions
    assert 'data-task="recruitment_mail_process"' in quick_questions
    assert "处理邮件信息" in quick_questions
    assert "163 邮件情报" not in quick_questions
    assert 'recruitment_mail_process: "处理邮件信息"' in js
    assert "处理全部待处理的招聘邮件" in html + js


def test_assistant_labels_knowledge_search_separately_from_company_coverage() -> None:
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'knowledge_search: "接入知识检索"' in js
    assert 'label: "接入知识检索"' in js
    assert js.index('label: "接入知识检索"') < js.index('label: "公司接入"')


def test_job_actions_and_read_only_previews_are_present() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
    for label in ("询问助理", "准备投递"):
        assert label in js
    assert "录阶预览" not in js + css
    assert "lujie" not in js.lower() + css.lower()
    assert "job-card" in js + css
    assert "today-todos" in html + js + css
    assert "只读预览" in js
    assert "不要写入或发送" in js
    assert "/api/codex/threads/${encodeURIComponent(state.codexThreadId)}/turns/stream" in js
    assert "/lujie/import" not in html + js + css


def test_recruitment_mail_view_is_preview_only() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'data-view-panel="mail"' in html
    assert "/api/recruitment-mails?limit=50" in js
    assert "/review" in js
    assert "邮件关联预览已生成，尚未写入" in js
    assert 'authHeaders("生成邮件关联预览"' in js


def test_assistant_persists_thread_and_recent_conversation_without_api_tokens() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="new-conversation-button"' in html
    assert 'id="clear-conversation-button"' in html
    assert "STORAGE_KEYS" in js
    assert "localStorage" in js
    assert "persistConversation" in js
    assert "codexThreadId" in js
    assert "assistantThreadId" not in js
    assert "persistClientMessage" not in js
    assert "STORAGE_SECRET_KEYS" in js
    assert "STORAGE_BULKY_KEYS" in js
    assert '"X-RecruitOps-Local-UI": "1"' in js
    assert "requestSessionToken" not in js
    assert '"/api/codex/threads?limit=20"' in js
    assert '/api/codex/threads/${encodeURIComponent(selectedThreadId)}' in js
    assert '/api/codex/threads/${encodeURIComponent(selectedThreadId)}/resume' in js
    assert "/api/assistant" not in js
    assert 'id="conversation-list"' in html
    assert 'id="assistant-stop-button"' in html
    assert 'id="assistant-security-button"' not in html
    assert "state.apiToken" not in js


def test_automations_view_reads_local_schedules_and_can_disable_them() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'data-view="automations"' in html
    assert 'id="automation-list"' in html
    assert 'id="automations-refresh-button"' in html
    assert 'api("/api/automations")' in js
    assert '/api/automations/${encodeURIComponent(scheduleId)}/disable' in js
    assert 'authHeaders("停用定时任务")' in js
    assert "renderAutomations" in js
    assert ".automation-item" in css


def test_assistant_clears_composer_when_submission_is_accepted() -> None:
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    start = js.index("async function submitAssistantQuestion")
    end = js.index("async function loadCore", start)
    submission = js[start:end]

    assert submission.index('$("assistant-message").value = "";') < submission.index('appendMessage("user", normalized)')
    assert submission.index('$("assistant-job-id").value = "";') < submission.index('appendMessage("user", normalized)')


def test_assistant_deletes_real_codex_threads_and_shows_compaction_policy() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert ">删除当前会话<" in html
    assert 'id="assistant-context-policy"' in html
    assert "async function deleteConversation" in js
    assert '/api/codex/threads/${encodeURIComponent(selectedThreadId)}' in js
    assert '{ method: "DELETE" }' in js
    assert "auto_compact_token_limit" in js
    assert "context_window_tokens" in js
    assert ".conversation-delete-button" in css
    assert ".context-policy-label" in css


def test_codex_turn_refreshes_the_approval_queue() -> None:
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'normalizeApprovals(await api("/api/approvals"))' in js
    assert "turn_completed" in js
    assert "codex_events" in js


def test_codex_events_render_operation_progress_without_review_polling() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'data-codex-status="progress"' in html
    assert 'setCodexEventStatus("progress"' in js
    assert 'kind === "text_delta"' in js
    assert 'kind === "turn_completed"' in js
    assert "ensureReviewProgressPolling" not in js
    assert "review_progress" not in js
    assert "/api/browser/application-status/reviews" not in js
    assert ".review-progress" not in css


def test_assistant_messages_render_plan_stages_tools_evidence_and_approvals() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "id=\"assistant-messages\"" in html
    assert "eventStages" in js
    assert "event_type" in js
    assert "response.plan" not in js
    assert "response.steps" not in js
    assert "renderExecutionStages" in js
    assert "renderInlineApprovals" in js
    assert "createApprovalFromPreview" in js
    assert 'button.textContent = "创建审批"' in js
    assert "data-approval-id" in js
    assert "decideApproval" in js
    assert "executeApproval" in js
    assert "tool_response" in js
    assert "grounded_evidence" in js
    assert "execution-stages" in css
    assert "inline-approval-card" in css
    assert "message-result-details" in js + css
    assert "visibleMessages = state.messages.slice(-50)" in js
    assert "result-load-more" in js + css
    assert 'response.status === "no_results"' in js


def test_quick_commands_share_the_assistant_submission_path_and_operational_report() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'class="command-button"' in html
    assert "submitAssistantQuestion(message" in js
    assert "data-task" in js
    assert "runAgentTask" not in js
    assert "/api/codex/threads/${encodeURIComponent(state.codexThreadId)}/turns/stream" in js
    assert "/api/reports/operational?on=" in js
    assert "repair_candidates" in js


def test_assistant_streams_progress_and_renders_domain_results() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'id="assistant-live-run"' in html
    assert 'Accept: "text/event-stream"' in js
    assert "streamResponse.body.getReader()" in js
    assert 'kind === "thread_started"' in js
    assert 'kind === "turn"' in js
    assert 'kind === "turn_started"' in js
    assert 'kind === "item_started"' in js
    assert 'kind === "text_delta"' in js
    assert 'kind === "turn_completed"' in js
    assert "renderMarkdown" in js
    assert "isMarkdownTable" in js
    assert "message-table-wrap" in js
    assert "/api/codex/threads/${encodeURIComponent(state.codexThreadId)}/events" in js
    assert "CODEX_STREAM_MAX_RECONNECTS" in js
    assert '"Last-Event-ID"' in js
    assert ".message-table-wrap table" in css
    assert "SAFE_LINK_PROTOCOLS" in js
    assert "AbortController" in js
    assert "controller.abort()" in js
    assert "renderBusinessRecord" in js
    for kind in ("job", "schedule", "application", "mail", "company"):
        assert f'kind === "{kind}"' in js
    assert ".business-result-row" in css


def test_codex_stream_uses_one_persistent_thread_and_real_event_deltas() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'api("/api/codex/health")' in js
    assert 'api("/api/codex/threads"' in js
    assert 'state.codexEnabled === true' in js
    assert 'state.codexEnabled === false' in js
    assert 'runLegacyAssistantQuery' not in js
    assert 'runAgentTask' not in js
    assert '/api/assistant' not in js
    assert '/api/browser/application-status/reviews' not in js
    assert '/api/codex/threads/${encodeURIComponent(state.codexThreadId)}/turns/stream' in js
    assert 'body: JSON.stringify({ text: message })' in js
    assert 'kind === "text_delta"' in js
    assert 'streamedAnswer += delta' in js
    assert 'codexThreadId' in js
    assert 'thread_id: state.codexThreadId' in js
    assert 'data-codex-status="thread"' in html
    for status in ("turn", "item", "tool", "progress", "error"):
        assert f'data-codex-status="{status}"' in html
    assert 'stopAssistantExecution' in js
    assert '/api/codex/threads/${encodeURIComponent(state.codexThreadId)}/interrupt' in js
    assert 'body: JSON.stringify({ turn_id: turnId })' in js
    assert 'eventStages' in js
    assert 'event_type' in js
    assert '.codex-event-chip' in css


def test_codex_history_ui_selects_by_thread_id_without_creating_per_turn() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'button.dataset.threadId = threadId || ""' in js
    assert 'loadConversation(threadId)' in js
    assert '"/api/codex/threads?limit=20"' in js
    assert 'cursor=${encodeURIComponent(cursor)}' in js
    assert "next_cursor" in js
    assert "nextCursor" in js
    assert '/api/codex/threads/${encodeURIComponent(selectedThreadId)}' in js
    assert '/api/codex/threads/${encodeURIComponent(selectedThreadId)}/resume' in js
    assert 'codexHistoryMessages(historyThread)' in js
    assert 'Array.isArray(thread?.turns)' in js
    assert 'state.conversations = state.codexThreadId' not in js
    assert 'thread_id: state.codexThreadId' in js
    assert 'body: JSON.stringify({ text: message })' in js
    assert "const loaded = await loadConversation(targetThreadId)" in js
    assert "await loadConversation(firstThreadId)" in js
    assert 'id="conversation-list"' in html
    assert 'id="conversation-list-controls"' in html
    assert 'id="conversation-load-more-button"' in html
    assert ".conversation-list-status" in css


def test_dashboard_approval_metric_uses_normalized_approval_state() -> None:
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'setText("metric-approvals", state.approvals.filter(' in js
    assert 'setText("metric-approvals", approvals.filter(' not in js


def test_codex_stream_errors_never_fall_back_to_success_wording() -> None:
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'payload?.text' in js
    assert 'data?.error?.message' in js
    assert 'payload?.error?.message' in js
    assert '"模型运行时返回了未说明的错误。"' in js
    assert '"Agent 专用 DeepSeek API 余额不足，请充值或更换 Key。"' in js
    assert ') || "已更新"' not in js
