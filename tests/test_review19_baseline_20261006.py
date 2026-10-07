"""Read-only baseline and precise entry diagnostics; no external services."""
import asyncio
from types import SimpleNamespace
import pytest
from packages import config
from packages.storage import ApplicationSnapshot
from packages.tools import batch_browser_operations as batch
from packages.tools.application_review_results import compact_summary
from tests.test_application_page_model_fallback import case, candidate, URL
from tests.test_review_whole_card_evidence import reading
from tests.test_batch_browser_operations import _repository, _observed


@pytest.mark.parametrize('confidence,allowed', [(.85, True), (.88, True), (.79, False), (.30, False)])
def test_source_verified_dated_submission_is_read_only(tmp_path, monkeypatch, confidence, allowed):
    title, label = '机器人系统工程师（应用）', '投递简历'
    text = f'{title}\n投递简历 2026-08-16'
    vision = {**reading(title, text, label, current=False), 'confidence': confidence}
    repository, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{'id': '24', 'title': title, 'record_url': URL}],
        observation={'vision': vision}, candidates=[candidate(title, label=label, quote=text,
            ref='vision:card:0', current=False, observed_status='applied')])
    row = run(visual=True)['24']
    assert row['state'] == ('unchanged' if allowed else 'unresolved'), row
    assert not row.get('wrote')
    with repository.storage.session() as session:
        app = session.get(ApplicationSnapshot, '24')
        assert app.stage == 'applied' and not app.stage_history


def test_lower_read_only_threshold_cannot_change_stage(tmp_path, monkeypatch):
    title, label = '测试工程师', '笔试中'
    text = f'{title}\n当前状态：{label}'
    repository, _, _, _, run = case(tmp_path, monkeypatch,
        observation={'vision': {**reading(title, text, label), 'confidence': .88}},
        applications=[{'id': '24', 'title': title, 'record_url': URL}],
        candidates=[candidate(title, label=label, quote=text, ref='vision:card:0')])
    assert run(visual=True)['24']['state'] == 'unresolved'
    assert repository.list_applications()[0].stage == 'applied'


@pytest.mark.parametrize('truncated', [False, True])
def test_resume_routing_zero_parser_confidence_not_a_stage_change(tmp_path, monkeypatch, truncated):
    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(
        write_enabled=True, llm_enabled=True, llm_api_key='fixture', vision_enabled=True))
    repository = _repository(tmp_path, [{'id': '24', 'title': 'AI应用工程师', 'record_url': URL}])
    card = {'title': 'AI应用工程师', 'context': 'AI应用工程师 当前进度：分配简历-流程中',
            'label': '分配简历-流程中', 'confidence': 0, 'status': '', 'signals': {'context_truncated': truncated}}
    calls = []
    async def observe(request, *_):
        calls.append(request.include_vision)
        return _observed({'application_records': [card], 'entries': [], 'page': {'text': card['context']}})
    monkeypatch.setattr(batch, 'observe_application_status_page_workflow', observe)
    response = asyncio.run(batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
        application_ids=['24']), object(), repository))
    if truncated:
        assert calls == [False, True] and not response.unchanged
    else:
        assert calls == [False] and response.unchanged[0].reason == 'no_newer_status_observed'


@pytest.mark.parametrize('code', ['APPLICATION_RECORD_ENTRY_NOT_ENTERED', 'APPLICATION_RECORD_HOME_REDIRECT'])
def test_entry_fault_is_precise_not_login_or_retained(tmp_path, monkeypatch, code):
    repository = _repository(tmp_path, [{'id': '24', 'title': 'AI应用工程师', 'record_url': URL}])
    async def observe(*_):
        return SimpleNamespace(data=SimpleNamespace(error_code=code, status='STATE_UNCLEAR',
            operation_id='entry', result={'navigation_diagnostics': {'reason': code.lower()}}, observation=None))
    monkeypatch.setattr(batch, 'observe_application_status_page_workflow', observe)
    response = asyncio.run(batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
        application_ids=['24']), object(), repository))
    assert response.unresolved[0].reason == code.lower()
    assert response.unresolved[0].presentation_state is None
    assert not response.blocked and not response.failed


def test_display_counts_are_disjoint_not_raw_unresolved():
    summary = compact_summary({'updated': 0, 'unchanged': 183, 'excluded': 12, 'blocked': 10,
        'unresolved': 19, 'failed': 3, 'retained_count': 16, 'attention_required_count': 3})
    assert sum(summary['display_counts'].values()) == 227
    assert summary['display_counts']['无法确认，需处理'] == 3


@pytest.mark.parametrize('saved,observed,label,allowed', [
    ('applied', 'applied', '投递简历 2026-09-23', True),
    ('applied', 'written', '当前状态：笔试中', False),
    ('interview1', 'applied', '投递简历 2026-09-23', False),
])
def test_read_only_verification_cannot_become_stage_write(tmp_path, monkeypatch, saved, observed, label, allowed):
    from packages.tools.application_status_evidence import VerifyApplicationStatusEvidenceInput, verify_application_status_evidence
    title = '测试工程师'
    text = f'{title}\n{label}'
    repository, store, operation, _, _ = case(tmp_path, monkeypatch,
        applications=[{'id': '24', 'title': title, 'record_url': URL, 'stage': saved}],
        observation={'vision': reading(title, text, label, current=True)})
    response = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id='24', observation_operation_id=operation.operation_id,
        observed_status=observed, observed_label=label, evidence=text, confidence=.98,
        captured_at='2026-09-29T01:00:00Z', source_ref='vision:card:0', source_title=title,
        current=True, read_only=True), store)
    assert response.success is allowed, response
    assert response.read_only
    if not allowed:
        assert response.reason_code == 'read_only_baseline_not_confirmed'
    with repository.storage.session() as session:
        app = session.get(ApplicationSnapshot, '24')
        assert app.stage == saved and not app.stage_history
