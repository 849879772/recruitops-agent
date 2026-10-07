"""Retention never touches business state or live/referenced evidence."""
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from packages.browser_bridge.retention import compact_browser_diagnostics, MARKER
from packages.storage import Storage
from packages.storage.models import (BrowserOperation, BrowserOperationEvent, BrowserOutbox,
    TaskRun, ToolCall, Approval, WriteAudit, ApplicationSnapshot, ApplicationIdentityBinding)

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


@pytest.fixture
def storage(tmp_path):
    value = Storage.from_url(f"sqlite:///{tmp_path / 'retention.sqlite'}", initialize=True)
    yield value
    value.engine.dispose()


def operation(storage, name='one', days=45, status='SUCCEEDED', ack=True, kind='observe_application_status_page'):
    then = NOW - timedelta(days=days)
    with storage.write_transaction() as s:
        s.add(BrowserOperation(operation_id=name, idempotency_key='key-' + name, operation=kind,
            device_id=name, status=status, command={'application_ids':['app'], 'page_url':'https://example.test'},
            result={'page':{'text':'private page'},'vision':{'text':'private OCR'}},
            last_event_sequence=2, completed_at=then, created_at=then, updated_at=then))
        s.flush()
        s.add_all([BrowserOperationEvent(event_id=name+str(i), operation_id=name, sequence=i,
            status=status, event_type=event, payload={'text':'private payload'}, occurred_at=then)
            for i,event in [(1,'vision_analysis'),(2,'status_model_result')]])
        s.add(BrowserOutbox(outbox_id=name, device_id=name, sequence=1, operation_id=name,
            message_type='command',payload={'text':'private command'}, ack_payload={'text':'private ack'},
            acked_at=then if ack else None))


def snapshot(storage):
    with storage.session() as s:
        return {cls.__tablename__: [{c.name:getattr(row,c.name) for c in cls.__table__.columns}
            for row in s.scalars(select(cls))] for cls in [BrowserOperation,BrowserOperationEvent,BrowserOutbox,
                TaskRun,ToolCall,Approval,WriteAudit,ApplicationSnapshot,ApplicationIdentityBinding]}


def sweep(storage, **kw):
    return compact_browser_diagnostics(storage, enabled=True, dry_run=False, now=NOW, **kw)


def test_disabled_and_dry_run_do_not_change_any_data(storage):
    operation(storage)
    before = snapshot(storage)
    assert not compact_browser_diagnostics(storage, now=NOW)['enabled']
    assert compact_browser_diagnostics(storage, enabled=True, now=NOW)['selected'] == 1
    assert snapshot(storage) == before


def test_twelve_hour_compaction_preserves_ids_counts_and_command(storage):
    operation(storage)
    before = snapshot(storage)
    assert sweep(storage)['compacted'] == 1
    after = snapshot(storage)
    op = after['browser_operations'][0]
    assert op['result'][MARKER] and op['result']['sha256']
    assert op['command'] == before['browser_operations'][0]['command']
    assert op['idempotency_key'] == 'key-one'
    assert len(after['browser_operation_events']) == 1
    assert all(e['payload'][MARKER] for e in after['browser_operation_events'])
    assert after['browser_outbox'] == []
    assert 'private' not in str(op['result'])


def test_events_merge_and_repeat_is_idempotent(storage):
    operation(storage, days=100)
    result = sweep(storage)
    assert result['events_removed'] == 1 and result['outbox_removed'] == 1
    first = snapshot(storage)
    event = first['browser_operation_events'][0]
    assert event['event_type'] == 'retention_summary'
    assert event['payload']['event_counts'] == {'vision_analysis':1,'status_model_result':1}
    result = compact_browser_diagnostics(storage,enabled=True,dry_run=False,now=NOW+timedelta(days=2))
    assert result['compacted'] == 0
    assert snapshot(storage)['browser_operation_events'] == first['browser_operation_events']


def test_compaction_ages_into_event_merge(storage):
    operation(storage)
    sweep(storage)
    result = compact_browser_diagnostics(storage,enabled=True,dry_run=False,now=NOW+timedelta(days=60))
    assert result['events_removed'] == 0
    assert snapshot(storage)['browser_operation_events'][0]['payload']['event_counts']['vision_analysis'] == 1


@pytest.mark.parametrize('params', [{'days':0.49},{'days':0.5},{'ack':False},{'status':'EXTRACTING'},
                                 {'kind':'other_operation'}])
def test_recent_active_pending_and_other_operations_are_preserved(storage, params):
    operation(storage, **params)
    before = snapshot(storage)
    assert sweep(storage)['compacted'] == 0
    after = snapshot(storage)
    assert after['browser_operation_events'] == before['browser_operation_events']
    assert after['browser_operations'][0]['result'] == before['browser_operations'][0]['result']


def task(s, name='task', status='completed'):
    s.add(TaskRun(id=name,status=status,task_type='review',user_request='fixture',source='test',source_ref=name))
    s.flush()


def test_running_task_prevents_maintenance(storage):
    operation(storage)
    with storage.write_transaction() as s:
        task(s,status='running')
    before = snapshot(storage)
    assert sweep(storage)['skipped_active']
    assert snapshot(storage) == before


@pytest.mark.parametrize('pin', ['approval','resumable','unknown_checkpoint'])
def test_referenced_evidence_is_not_compacted(storage,pin):
    operation(storage)
    with storage.write_transaction() as s:
        task(s)
        s.add(ApplicationSnapshot(id='app',company_name='示例',job_title='工程师',stage='applied',
            idempotency_key='application',stage_history=[],source='test',source_ref='app'))
        s.flush()
        if pin == 'approval':
            s.add(Approval(id='approval',task_id='task',operation='bind',idempotency_key='approval',
                preview={'observation_operation_id':'one'},status='pending',source='test',source_ref='approval'))
        elif pin == 'write':
            s.add(WriteAudit(execution_id='audit',token_id='token',task_id='task',operation='application_stage_update',
                idempotency_key='audit',operator='test',evidence=[{'source_ref':'one'}],
                before_diff={'stage':'applied'},after_diff={'stage':'written'},started_at=NOW,completed_at=NOW,success=True))
        elif pin == 'binding':
            s.add(ApplicationIdentityBinding(application_id='app',state='confirmed',identity_digest='digest',
                page_url='https://example.test',card={},operation_id='one',approval_key='binding'))
        else:
            s.add(ToolCall(id='checkpoint',task_id='task',tool_name='application_review_checkpoint',
                arguments={'run_status':'completed' if pin=='latest' else 'paused' if pin=='resumable' else 'unknown',
                           'results':{'app':{'operation_id':'one'}}},updated_at=NOW-timedelta(days=2),source='test',source_ref='checkpoint'))
            if pin != 'latest':
                s.add(ToolCall(id='newer',task_id='task',tool_name='application_review_checkpoint',
                    arguments={'run_status':'completed','results':{},'latest_reviews_saved':True},updated_at=NOW,source='test',source_ref='newer'))
    before = snapshot(storage)
    assert sweep(storage)['compacted'] == 0
    after = snapshot(storage)
    for table in before:
        assert after[table] == before[table]
    assert after['browser_operations'][0]['result'] == before['browser_operations'][0]['result']


def test_pinned_and_unacked_rows_cannot_starve_batch(storage):
    operation(storage,'aaa-pending',days=100,ack=False)
    operation(storage,'bbb-pinned',days=100)
    operation(storage,'zzz-eligible',days=50)
    with storage.write_transaction() as s:
        task(s)
        s.add(Approval(id='pending',task_id='task',operation='bind',idempotency_key='approval',
            preview={'operation_id':'bbb-pinned'},status='pending',source='test',source_ref='pending'))
    assert sweep(storage,batch_size=1)['compacted']==1
    with storage.session() as s:
        assert s.get(BrowserOperation,'zzz-eligible').result[MARKER]
        assert not s.get(BrowserOperation,'bbb-pinned').result.get(MARKER)


def test_batch_limit_and_transaction_rollback(storage,monkeypatch):
    operation(storage,'one')
    operation(storage,'two')
    assert sweep(storage,batch_size=1)['compacted'] == 1
    before = snapshot(storage)
    import packages.browser_bridge.retention as retention
    original = retention._summary
    def fail(value,when):
        if value.get('page'):
            raise RuntimeError('failure midway')
        return original(value,when)
    monkeypatch.setattr(retention,'_summary',fail)
    with pytest.raises(RuntimeError):
        sweep(storage)
    assert snapshot(storage) == before


def test_expired_evidence_cannot_write_status(storage):
    from packages.browser_bridge import BrowserBridgeStore
    from packages.tools.application_status_evidence import VerifyApplicationStatusEvidenceInput, verify_application_status_evidence
    operation(storage)
    sweep(storage)
    result = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(application_id='app',
        observation_operation_id='one',observed_status='applied',observed_label='已投递',evidence='已投递',confidence=1,captured_at=NOW.isoformat()),
        BrowserBridgeStore(storage))
    assert not result.success and result.error_code == 'observation_evidence_expired'


@pytest.mark.parametrize('enabled',[False,True])
def test_maintenance_is_delayed_gated_and_disposes_connection(monkeypatch,enabled):
    import asyncio
    from types import SimpleNamespace
    from apps.api import main as api
    import packages.browser_bridge.retention as retention
    waits, calls, disposed = [], [], []
    async def sleep(delay):
        waits.append(delay)
        if delay==300:
            raise asyncio.CancelledError
    def open_storage(url):
        assert enabled and waits==[60] and url=='test-only'
        return SimpleNamespace(engine=SimpleNamespace(dispose=lambda: disposed.append(True)))
    def compact(storage,**kw):
        calls.append(kw)
        return {'compacted':1}
    monkeypatch.setattr(api.asyncio,'sleep',sleep)
    monkeypatch.setattr(api,'get_settings',lambda: SimpleNamespace(write_enabled=enabled,database_url='test-only'))
    monkeypatch.setattr(api.Storage,'from_url',open_storage)
    monkeypatch.setattr(retention,'compact_browser_diagnostics',compact)
    state=SimpleNamespace()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(api._maintain_browser_diagnostics(state))
    assert waits==[60,300]
    assert bool(calls)==bool(disposed)==enabled
    if enabled:
        assert state.browser_diagnostics_retention=={'compacted':1}
        assert calls==[{'enabled':True,'dry_run':False}]
