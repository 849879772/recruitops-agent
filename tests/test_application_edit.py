from datetime import datetime, timezone
from types import SimpleNamespace
from pydantic import ValidationError
import pytest
from packages.storage import Storage, ApplicationSnapshot
from packages.tools.application_edit import ApplicationEditInput, edit_application_metadata
from packages.mcp.server import MCP_AGENT_TOOL_NAMES, TOOL_DEFINITIONS, _build_handler


def test_edit_is_exposed_and_preserves_progress(monkeypatch):
    storage = Storage.from_url('sqlite+pysqlite:///:memory:', initialize=True)
    now = datetime.now(timezone.utc)
    history = [{'stage': 'applied', 'note': 'original'}]
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id='test', company_name='D.T', job_title='Agent工程师',
            stage='applied', stage_history=history, source='fixture', source_ref='test',
            idempotency_key='test', updated_at=now))
    request = ApplicationEditInput(application_id='test', expected_updated_at=now, company_name='荣耀')
    assert 'application_edit' in MCP_AGENT_TOOL_NAMES
    import packages.mcp.server as module
    monkeypatch.setattr(module, 'get_settings', lambda: SimpleNamespace(write_enabled=True))
    definition = next(d for d in TOOL_DEFINITIONS if d.name == 'application_edit')
    handler = _build_handler(definition, SimpleNamespace(repository=SimpleNamespace(storage=storage)))
    result = handler(request)
    assert result.success and result.data['after'] == {'company_name': '荣耀'}
    assert handler(request).data['changed'] is False
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, 'test')
        assert row.stage == 'applied' and row.stage_history == history
        assert row.job_title == 'Agent工程师'
    stale = request.model_copy(update={'company_name':'另一公司'})
    assert not handler(stale).success
    assert not edit_application_metadata(request, storage, write_enabled=False).success


def test_edit_rejects_stage_and_unsafe_url():
    base = dict(application_id='test', expected_updated_at=datetime.now(timezone.utc))
    for patch in ({'stage':'rejected'}, {'record_url':'javascript:alert(1)'}, {'company_name':None}, {}):
        with pytest.raises(ValidationError):
            ApplicationEditInput(**base, **patch)
