from scripts.fetch_offerbiu_quality_snapshot import capture


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.cookies = {}
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        payload = next(self.responses)
        return type('Response', (), {'status_code': 200, 'json': lambda self: payload})()


def page(index, ids, limited=False):
    return {'success': True, 'data': {'page': index, 'size': 9, 'totalItems': 2,
        'totalPages': 2, 'previewLimited': limited,
        'items': [{'id': key, 'recruitType': '秋招', 'targetYears': [2027],
                   'industryGroupCodes': ['internet-tech']} for key in ids]}}


def test_complete_capture(tmp_path):
    result = capture(tmp_path, session=FakeSession([page(0,['a']),page(1,['b'])]), delay=0)
    assert result['complete']
    assert len(result['items']) == 2


def test_limit_stops_without_attempting_more_pages(tmp_path):
    session = FakeSession([page(0,['a']),page(1,['b'],True)])
    result = capture(tmp_path,session=session,delay=0)
    assert not result['complete']
    assert result['stop_reason'] == 'preview_or_access_limit'
    assert session.calls == 2


def test_repeated_page_does_not_claim_complete(tmp_path):
    result = capture(tmp_path,session=FakeSession([page(0,['a']),page(1,['a'])]),delay=0)
    assert result['stop_reason'] == 'duplicate_or_missing_record_id'
    assert not result['complete']
