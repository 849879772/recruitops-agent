"""Latest receipts outlive diagnostics without becoming a second history."""
from datetime import timedelta, datetime, timezone
from time import perf_counter

import pytest
from sqlalchemy import select

from packages.storage.application_reviews import save_latest_reviews, safe_review_navigation_diagnostics
from packages.storage.models import ApplicationSnapshot, TaskRun, ToolCall, WriteAudit, BrowserOperation, ApplicationIdentityBinding
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.browser_bridge.retention import compact_browser_diagnostics, MARKER
from packages.tools.application_review_run import _response
from packages.tools.application_review_tasks import review_summary
from test_browser_diagnostic_retention import storage, operation, NOW, snapshot, sweep, task
from test_application_identity_binding import case, confirm, URL


def app(session):
    session.add(ApplicationSnapshot(id='app', company_name='示例', job_title='工程师', stage='applied',
        idempotency_key='app', source='fixture', source_ref='app', updated_at=NOW-timedelta(days=10),
        stage_history=[{'stage':'applied','at':'2026-09-01'}]))
    session.flush()


def receipt(**kw):
    return {'application_id':'app', 'state':'unchanged', 'reason':'verified', 'operation_id':'one',
            'elapsed_ms':0, 'observation':{'text':'private page'}, 'diagnostics':{'raw':'private'}, **kw}


def test_latest_overwrites_without_history_growth_and_replay_cannot_regress(storage):
    with storage.write_transaction() as s:
        app(s)
        save_latest_reviews(s, [receipt()], checked_at=NOW, run_id='first')
        save_latest_reviews(s, [receipt(state='failed', reason='timeout')], checked_at=NOW+timedelta(hours=1), run_id='new')
        save_latest_reviews(s, [receipt()], checked_at=NOW-timedelta(hours=1), run_id='old')
    row = PostgresRecruitmentRepository(storage).list_applications()[0]
    assert row.last_review['state'] == 'failed' and row.last_review['run_id'] == 'new'
    assert len(row.stage_history) == 1 and row.stage.value == 'applied'
    assert row.updated_at.replace(tzinfo=timezone.utc) == NOW-timedelta(days=10)
    assert 'private' not in str(row.last_review)
    assert PostgresRecruitmentRepository(storage).search_applications().items[0].last_review == row.last_review
    from packages.storage.sync import upsert_application_snapshot
    with storage.write_transaction() as s:
        upsert_application_snapshot(s, row.model_copy(update={'last_review':None}))
    assert PostgresRecruitmentRepository(storage).list_applications()[0].last_review == row.last_review


@pytest.mark.parametrize('status', ['completed', 'cancelled'])
def test_completed_details_expire_but_latest_and_terminal_receipt_survive(storage, status):
    operation(storage, days=1)
    with storage.write_transaction() as s:
        app(s)
        task(s, status=status)
        s.add(ToolCall(id='cp', task_id='task', tool_name='application_review_checkpoint', source='test',
            arguments={'ids':['app'], 'results':{'app':receipt()}, 'run_status':status, 'attempts':{'app':1}},
            updated_at=NOW-timedelta(hours=13)))
    assert sweep(storage)['checkpoints_compacted'] == 1
    with storage.session() as s:
        cp = s.get(ToolCall, 'cp')
        assert 'results' not in cp.arguments and 'ids' not in cp.arguments
        assert cp.updated_at.replace(tzinfo=timezone.utc) == NOW-timedelta(hours=13)
        latest = s.get(ApplicationSnapshot,'app').last_review
        assert latest['checked_at'] == (NOW-timedelta(hours=13)).isoformat()
        assert latest['state']=='unchanged' and 'private' not in str(latest)
        summary = review_summary('task', cp.arguments)
        assert summary['total']==1 and summary['details_expired'] and summary['actions']==[]
        result = _response('task', cp.arguments, perf_counter())
        assert not result.success and not result.unchanged and not summary['continuation_required']
    before = snapshot(storage)
    assert sweep(storage)['compacted']==0
    assert snapshot(storage)==before


@pytest.mark.parametrize('reference', ['write', 'binding'])
def test_actual_history_and_bindings_survive_but_do_not_pin_entire_page(storage, reference):
    operation(storage, days=0.51)
    with storage.write_transaction() as s:
        app(s)
        task(s)
        if reference=='write':
            s.add(WriteAudit(execution_id='audit',token_id='token',task_id='task',operation='application_stage_update',
                idempotency_key='audit',operator='test',evidence=[{'source_ref':'one','quote':'笔试'}],
                before_diff={'stage':'applied'},after_diff={'stage':'written'},rollback_payload={'stage':'applied'},
                started_at=NOW,completed_at=NOW,success=True))
        else:
            s.add(ApplicationIdentityBinding(application_id='app',state='bound',identity_digest='digest',
                page_url='https://example.test',card={'raw_title':'工程师'},operation_id='one',approval_key='binding'))
    before=snapshot(storage)
    assert sweep(storage)['compacted']==1
    after=snapshot(storage)
    for key in ('write_audits','application_snapshots','application_identity_bindings'):
        assert before[key]==after[key]


def test_pending_identity_retains_only_candidate_identity_and_can_be_confirmed(case):
    storage, _, _ = case
    now=datetime.now(timezone.utc)
    with storage.write_transaction() as s:
        op=s.get(BrowserOperation,'op')
        op.created_at=op.completed_at=op.updated_at=now-timedelta(hours=13)
        save_latest_reviews(s, [{'application_id':'a','state':'unresolved','reason':'target_record_not_matched','operation_id':'op'}], checked_at=now-timedelta(hours=13))
    result=compact_browser_diagnostics(storage,enabled=True,dry_run=False,now=now)
    assert result['compacted']==1
    with storage.session() as s:
        evidence=s.get(BrowserOperation,'op').result
        assert evidence[MARKER] and evidence['retention_level']=='identity'
        assert 'context' not in evidence['application_records'][0]
        assert 'label' not in evidence['application_records'][0]
    from packages.tools.application_identity_binding import application_identity_queue
    assert application_identity_queue(storage)['total']==1
    assert confirm(case).success
    assert application_identity_queue(storage)['total']==0


def test_old_checkpoint_cannot_replace_more_recent_latest(storage):
    with storage.write_transaction() as s:
        app(s)
        task(s)
        save_latest_reviews(s, [receipt(state='failed')], checked_at=NOW)
        s.add(ToolCall(id='cp', task_id='task', tool_name='application_review_checkpoint', source='test',
            arguments={'ids':['app'], 'results':{'app':receipt()}, 'run_status':'completed'},updated_at=NOW-timedelta(days=2)))
    sweep(storage)
    assert PostgresRecruitmentRepository(storage).list_applications()[0].last_review['state']=='failed'


@pytest.mark.parametrize('provider,phase', [('alibaba', 'initial_load'), ('huawei', 'navigation_recovery')])
def test_latest_preserves_bounded_sso_cause_not_raw_auth_or_page_data(storage, provider, phase):
    origin = 'https://mozi-login.alibaba-inc.com' if provider == 'alibaba' else 'https://uniportal.huawei.com'
    navigation = {'reason': 'will_redirect_official_sso', 'phase': phase, 'sameOrigin': False,
        'ssoCandidate': True, 'finalUrl': origin + '/ssoLogin.htm?token=private',
        'requestedUrl': 'https://career.example/applications/person-private?code=secret',
        'authNavigation': {'provider': provider, 'hops': 2, 'returnedToRecruitment': False, 'token': 'private'},
        'authWait': {'outcome': 'timeout', 'elapsedMs': 15001, 'budgetMs': 15000, 'progressCount': 1,
                     'raw': 'private'}, 'page': {'text': 'private page'}, 'unknown': 'private'}
    with storage.write_transaction() as s:
        app(s)
        save_latest_reviews(s, [receipt(state='failed', reason='authentication_recovery_timeout',
            diagnostics={'navigation_diagnostics': navigation, 'raw': 'private'})], checked_at=NOW)
    latest = PostgresRecruitmentRepository(storage).list_applications()[0].last_review
    safe = latest['diagnostics']['navigation_diagnostics']
    assert safe['phase'] == phase
    assert safe['authNavigation'] == {'provider': provider, 'hops': 2, 'returnedToRecruitment': False}
    assert safe['authWait'] == {'outcome': 'timeout', 'elapsedMs': 15001, 'budgetMs': 15000, 'progressCount': 1}
    assert safe['requestedUrl'] == 'https://career.example/applications/[redacted]'
    assert safe['finalUrl'] == origin + '/ssoLogin.htm'
    assert 'private' not in str(latest) and 'secret' not in str(latest)
    assert set(latest['diagnostics']) == {'navigation_diagnostics'}


def test_navigation_projection_rejects_credentials_free_text_and_malformed_counts():
    projected = safe_review_navigation_diagnostics({
        'reason': 'private arbitrary page text', 'phase': 'private', 'restriction': 'private',
        'requestedUrl': 'https://user:private@example.test/applications',
        'finalUrl': 'https://example.test/applications/secret-person?token=private#secret',
        'sameOrigin': 'true', 'ssoCandidate': True, 'reobservationCount': True,
        'authNavigation': {'provider': [], 'hops': 'private', 'returnedToRecruitment': 'private'},
        'authWait': {'outcome': [], 'elapsedMs': True}, 'raw': 'private',
    })
    assert projected == {'ssoCandidate': True, 'finalUrl': 'https://example.test/applications/[redacted]'}
    assert safe_review_navigation_diagnostics(['private']) == {}
