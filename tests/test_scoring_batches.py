from types import SimpleNamespace

import pytest

from packages.matching.models import DecisionAction
from packages.matching.rules import content_fingerprint, profile_fingerprint
from scripts.prepare_scoring_batches import prepare


def manifest():
    jobs = [{'id': str(i), 'company': 'Example', 'title': 'Developer',
             'jd_raw': 'Complete source text', 'cohort': 2027,
             'cohort_status': 'confirmed', 'batch': 'formal'} for i in range(3)]
    for job in jobs:
        job['content_fingerprint'] = content_fingerprint(job)
    return {'run_id': 'test', 'profile': {}, 'profile_fingerprint': profile_fingerprint({}), 'jobs': jobs}


def test_batches_preserve_full_input_and_bound_rows():
    batches, excluded = prepare(manifest(), max_jobs=2, max_characters=100_000)
    assert [len(b['jobs']) for b in batches] == [2, 1]
    assert batches[0]['jobs'][0]['jd_raw'] == 'Complete source text'
    assert not excluded


def test_batches_preserve_capture_contract_for_validator():
    data = manifest()
    data['jobs'][0].update({
        'source': 'offerbiu.title_first',
        'source_ref': 'source-1',
        'capture_status': 'complete',
        'capture_failure_reason': None,
        'capture_evidence': {
            'status': 'complete',
            'identity_verified': True,
            'terminal_observed': True,
        },
    })

    batches, _ = prepare(data, max_jobs=3, max_characters=100_000)

    queued = batches[0]['jobs'][0]
    assert queued['source'] == 'offerbiu.title_first'
    assert queued['source_ref'] == 'source-1'
    assert queued['capture_status'] == 'complete'
    assert queued['capture_failure_reason'] is None
    assert queued['capture_evidence']['identity_verified'] is True


def test_changed_content_or_profile_is_rejected():
    data = manifest()
    data['jobs'][0]['jd_raw'] = 'Changed'
    with pytest.raises(ValueError, match='Job fingerprint'):
        prepare(data, max_jobs=2, max_characters=100_000)
    data = manifest()
    data['profile']['degree'] = 'Changed'
    with pytest.raises(ValueError, match='Profile fingerprint'):
        prepare(data, max_jobs=2, max_characters=100_000)


def test_duplicates_and_invalid_limits_are_rejected():
    data = manifest()
    data['jobs'].append(data['jobs'][0])
    with pytest.raises(ValueError, match='Duplicate'):
        prepare(data, max_jobs=2, max_characters=100_000)
    with pytest.raises(ValueError, match='positive'):
        prepare(manifest(), max_jobs=0, max_characters=100_000)


def test_scoring_queue_does_not_repeat_direction_or_internship_screening():
    data = manifest()
    data['jobs'][0]['title'] = 'C++软件开发实习生'
    data['jobs'][0]['content_fingerprint'] = content_fingerprint(data['jobs'][0])
    batches, excluded = prepare(data, max_jobs=3, max_characters=100_000)
    assert [job['id'] for batch in batches for job in batch['jobs']] == ['0', '1', '2']
    assert not excluded


def test_assigned_jobs_are_not_dispatched_twice():
    batches, excluded = prepare(manifest(), max_jobs=2, max_characters=100_000,
                                 assigned_ids={'1'})
    assert [job['id'] for batch in batches for job in batch['jobs']] == ['0', '2']
    assert [(item['job_id'], item['status']) for item in excluded] == [('1', 'already_assigned')]


def test_complete_compatible_analysis_is_not_dispatched_again(monkeypatch):
    monkeypatch.setattr('scripts.prepare_scoring_batches.decide_reuse', lambda *a, **k: SimpleNamespace(
        action=DecisionAction.REUSE if a[0] == {'analysis_status': 'complete'} else DecisionAction.ANALYZE))
    data = manifest()
    data['jobs'][0]['previous_analysis'] = {'analysis_status': 'complete'}

    batches, excluded = prepare(
        data, max_jobs=3, max_characters=100_000, target_model='gpt-5.6-luna',
    )

    assert [job['id'] for batch in batches for job in batch['jobs']] == ['1', '2']
    assert [(item['job_id'], item['status']) for item in excluded] == [
        ('0', 'already_complete_reusable'),
    ]
