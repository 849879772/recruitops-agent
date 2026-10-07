"""File diagnostics are bounded, atomic, and independent from chat/database history."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest

from packages.codex_runtime.telemetry import CodexTrace, JsonlTraceRecorder
from packages.codex_runtime.trace_retention import prune_trace_file, trace_lock


def trace(**kwargs):
    return CodexTrace(event_type='test', method='turn/test', stage='tool', phase='completed', **kwargs)


def test_expired_lines_removed_recent_preserved_and_other_files_untouched(tmp_path):
    now=datetime.now(timezone.utc)
    path=tmp_path/'codex-traces.jsonl'
    chat=tmp_path/'conversation.json'
    chat.write_text('permanent chat',encoding='utf-8')
    recorder=JsonlTraceRecorder(path)
    recorder.record(trace(trace_id='old',observed_at=now-timedelta(hours=13)))
    recorder.record(trace(trace_id='boundary',observed_at=now-timedelta(hours=12)))
    recorder.record(trace(trace_id='recent',observed_at=now))
    with path.open('a',encoding='utf-8') as out:
        out.write('incomplete junk\n')
    result=prune_trace_file(path,now=now)
    assert result['after_bytes'] < result['before_bytes']
    assert [t.trace_id for t in recorder.read()] == ['boundary','recent']
    assert chat.read_text(encoding='utf-8')=='permanent chat'
    assert not list(tmp_path.glob('*.prune-*'))


def test_record_caps_existing_file_and_keeps_new_record(tmp_path,monkeypatch):
    from packages.codex_runtime import telemetry
    monkeypatch.setattr(telemetry,'TRACE_MAX_BYTES',3000)
    path=tmp_path/'codex-traces.jsonl'
    recorder=JsonlTraceRecorder(path)
    for i in range(50):
        recorder.record(trace(trace_id=str(i)))
    assert path.stat().st_size < 4000
    assert recorder.read(limit=1)[0].trace_id=='49'


def test_atomic_replace_failure_leaves_original_and_no_temp_file(tmp_path,monkeypatch):
    import packages.codex_runtime.trace_retention as retention
    path=tmp_path/'codex-traces.jsonl'
    JsonlTraceRecorder(path).record(trace())
    original=path.read_bytes()
    def fail(*args):
        raise PermissionError('synthetic file busy')
    monkeypatch.setattr(retention.os,'replace',fail)
    with pytest.raises(PermissionError):
        prune_trace_file(path)
    assert path.read_bytes()==original
    assert not list(tmp_path.glob('*.prune-*'))


def test_separate_recorders_and_maintenance_share_lock(tmp_path):
    path=tmp_path/'codex-traces.jsonl'
    def write(prefix):
        recorder=JsonlTraceRecorder(path)
        for i in range(40):
            recorder.record(trace(trace_id=f'{prefix}-{i}'))
            if i%10==0:
                prune_trace_file(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write,range(4)))
    rows=JsonlTraceRecorder(path).read(limit=200)
    assert len(rows)==160 and len({row.trace_id for row in rows})==160
    assert len(JsonlTraceRecorder(path).read(limit=2))==2


def test_record_storage_failure_does_not_abort_business_turn(tmp_path,monkeypatch):
    from packages.codex_runtime import telemetry
    def fail(*args):
        raise PermissionError('synthetic read-only disk')
    monkeypatch.setattr(telemetry,'trace_lock',fail)
    JsonlTraceRecorder(tmp_path/'codex-traces.jsonl').record(trace())


def test_missing_file_maintenance_does_not_create_any_file(tmp_path):
    assert prune_trace_file(tmp_path/'codex-traces.jsonl')=={'before_bytes':0,'after_bytes':0}
    assert list(tmp_path.iterdir())==[]
