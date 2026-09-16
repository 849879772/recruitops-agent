from hashlib import sha256

import pytest

from packages.domain.models import Company, Job, JobAnalysis
from packages.recruitment_core.jd_repair import content_sha256
from packages.recruitment_core.jd_repair_import import import_jd_repairs
from packages.storage import JobSnapshot, JobAnalysisSnapshot, Storage
from packages.storage.sync import upsert_company_snapshot, upsert_job_snapshot, upsert_job_analysis_snapshot


JD = ('岗位职责：负责机器人 C++ 核心系统开发、维护、测试和性能优化，参与方案设计与代码审查。\n'
      '任职要求：熟悉 C++ 和 Linux 软件开发，具备良好的编程能力和团队协作能力，能够独立完成问题定位。')
PROFILE = {'skills': ['C++'], 'matching': {'primary_directions': ['C++软件开发']}}


def capture_evidence(detail: str, source_url: str) -> dict[str, object]:
    return {
        'status': 'complete',
        'method': 'test_fixture',
        'source_url': source_url,
        'identity_verified': True,
        'terminal_observed': True,
        'remaining_controls': [],
        'content_sha256': content_sha256(detail),
    }


def setup_case(tmp_path, count=1):
    storage = Storage.from_url('sqlite+pysqlite:///:memory:', initialize=True)
    company = Company(id='c1', name='Example', campus_url='https://example.com/campus',
                      integration_status='connected', source='test')
    results = []
    with storage.transaction(write=True) as session:
        upsert_company_snapshot(session, company)
        for i in range(count):
            job = Job(id=f'j{i}', company_id='c1', title='C++软件开发工程师',
                      detail_url=f'https://example.com/jobs/j{i}', jd_raw='Truncated',
                      cohort=2027, cohort_status='confirmed', batch='formal', source='test')
            upsert_job_snapshot(session, job)
            upsert_job_analysis_snapshot(session, job, JobAnalysis(analysis_status='complete', match_score=99))
            results.append({'job_id': job.id, 'company_id': 'c1', 'company': 'Example', 'title': job.title,
                            'detail_url': job.detail_url, 'original_sha256': content_sha256(job.jd_raw),
                            'candidate_jd': JD, 'candidate_sha256': content_sha256(JD),
                            'validation': {'passed': True, 'status': 'complete', 'source': 'site_api',
                                           'detail_url': job.detail_url, 'identity_status': 'matched',
                                           'identity_evidence': [f'job_id:{job.id}'],
                                           'capture_evidence': capture_evidence(JD, job.detail_url)}})
    archive = tmp_path / 'backup.dump'
    archive.write_bytes(b'isolated-test-backup')
    return storage, {'results': results}, {'path': str(archive), 'sha256': sha256(archive.read_bytes()).hexdigest()}


def test_dry_run_does_not_change_stored_jd_or_score(tmp_path):
    storage, report, backup = setup_case(tmp_path)
    outcome = import_jd_repairs(storage, report, PROFILE, ['j0'])
    assert outcome['planned'] == 1 and outcome['written'] == 0
    with storage.session() as session:
        assert session.get(JobSnapshot, 'j0').jd_raw == 'Truncated'
        assert session.get(JobAnalysisSnapshot, 'j0').match_score == 99


def test_apply_clears_old_score_then_is_idempotent(tmp_path):
    storage, report, backup = setup_case(tmp_path)
    assert import_jd_repairs(storage, report, PROFILE, ['j0'], apply=True, backup=backup)['written'] == 1
    with storage.session() as session:
        row = session.get(JobSnapshot, 'j0')
        updated = row.updated_at
        assert row.jd_raw == JD and row.match_score is None
        assert row.capture_evidence == report['results'][0]['validation']['capture_evidence']
        analysis = session.get(JobAnalysisSnapshot, 'j0')
        assert analysis.analysis_status == 'eligible' and analysis.match_score is None
        assert analysis.model is None
    assert import_jd_repairs(storage, report, PROFILE, ['j0'], apply=True, backup=backup)['reused'] == 1
    with storage.session() as session:
        assert session.get(JobSnapshot, 'j0').updated_at == updated


@pytest.mark.parametrize('field,value,error', [
    ('company', 'Wrong', 'identity'), ('detail_url', 'https://example.com/jobs/other', 'identity'),
    ('original_sha256', '0' * 64, 'Original JD changed'),
    ('candidate_sha256', '0' * 64, 'checksum'),
])
def test_stale_or_wrong_identity_rejected(tmp_path, field, value, error):
    storage, report, backup = setup_case(tmp_path)
    report['results'][0][field] = value
    with pytest.raises(ValueError, match=error):
        import_jd_repairs(storage, report, PROFILE, ['j0'], apply=True, backup=backup)


def test_forged_passed_is_not_validation(tmp_path):
    storage, report, backup = setup_case(tmp_path)
    report['results'][0]['validation']['identity_status'] = 'mismatch'
    with pytest.raises(ValueError, match='Candidate not valid'):
        import_jd_repairs(storage, report, PROFILE, ['j0'], apply=True, backup=backup)


def test_second_row_failure_rolls_back_first_write(tmp_path):
    storage, report, backup = setup_case(tmp_path, count=2)
    report['results'][1]['original_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='Original JD changed'):
        import_jd_repairs(storage, report, PROFILE, ['j0', 'j1'], apply=True, backup=backup)
    with storage.session() as session:
        assert session.get(JobSnapshot, 'j0').jd_raw == 'Truncated'
        assert session.get(JobAnalysisSnapshot, 'j0').match_score == 99


def test_bad_backup_and_duplicate_ids_are_rejected(tmp_path):
    storage, report, backup = setup_case(tmp_path)
    with pytest.raises(ValueError, match='unique'):
        import_jd_repairs(storage, report, PROFILE, ['j0', 'j0'])
    backup['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='backup checksum'):
        import_jd_repairs(storage, report, PROFILE, ['j0'], apply=True, backup=backup)
