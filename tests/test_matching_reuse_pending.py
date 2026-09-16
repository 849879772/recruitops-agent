import pytest

from packages.matching.models import AnalysisRecord, AnalysisStatus, DecisionAction
from packages.matching.rules import content_fingerprint, profile_fingerprint
from packages.matching.service import ANALYSIS_VERSION, PROMPT_VERSION, decide_reuse


TARGET_MODEL = 'gpt-5.6-luna'


@pytest.mark.parametrize('status', [AnalysisStatus.ELIGIBLE, AnalysisStatus.JD_INCOMPLETE,
                                   AnalysisStatus.DIRECTION_OUT, AnalysisStatus.EARLY_BATCH,
                                   AnalysisStatus.REFUSED])
def test_pending_or_filtered_record_is_not_a_completed_score(status):
    job = {'id': 'test', 'title': 'C++ Developer'}
    profile = {'skills': ['C++']}
    previous = AnalysisRecord(job_id='test', analysis_status=status,
                              analysis_version=ANALYSIS_VERSION, prompt_version=PROMPT_VERSION,
                              content_fingerprint=content_fingerprint(job),
                              profile_fingerprint=profile_fingerprint(profile),
                              model=TARGET_MODEL)
    decision = decide_reuse(previous, job, profile, model=TARGET_MODEL)
    assert decision.action == DecisionAction.ANALYZE
    assert decision.reason == 'previous_analysis_not_complete'


def test_completed_unchanged_analysis_is_still_reused():
    job = {'id': 'test', 'title': 'C++ Developer'}
    profile = {'skills': ['C++']}
    previous = AnalysisRecord(job_id='test', analysis_status=AnalysisStatus.COMPLETE, match_score=70,
                              analysis_version=ANALYSIS_VERSION, prompt_version=PROMPT_VERSION,
                              content_fingerprint=content_fingerprint(job),
                              profile_fingerprint=profile_fingerprint(profile),
                              model=TARGET_MODEL)
    assert decide_reuse(previous, job, profile, model=TARGET_MODEL).action == DecisionAction.REUSE


def test_completed_raw_storage_row_with_json_text_lists_is_reused():
    job = {'id': 'test', 'title': 'C++ Developer'}
    profile = {'skills': ['C++']}
    previous = AnalysisRecord(
        job_id='test', analysis_status=AnalysisStatus.COMPLETE, match_score=70,
        advantages=['C++ project'], gaps=['CUDA'],
        analysis_version=ANALYSIS_VERSION, prompt_version=PROMPT_VERSION,
        content_fingerprint=content_fingerprint(job),
        profile_fingerprint=profile_fingerprint(profile), model=TARGET_MODEL,
    ).model_dump(mode='json')
    previous['advantages'] = '["C++ project"]'
    previous['gaps'] = '["CUDA"]'
    previous['source'] = 'recruitops-agent.daily_pipeline'
    previous['source_ref'] = 'job:test:analysis'

    decision = decide_reuse(previous, job, profile, model=TARGET_MODEL)

    assert decision.action == DecisionAction.REUSE
    assert decision.reason == 'existing_complete_score_reuse'


@pytest.mark.parametrize(
    ('stored_model', 'target_model', 'expected_action', 'expected_reason'),
    [
        (TARGET_MODEL, TARGET_MODEL, DecisionAction.REUSE, 'existing_complete_score_reuse'),
        (TARGET_MODEL, 'deepseek-test', DecisionAction.REUSE, 'existing_complete_score_reuse'),
        (TARGET_MODEL, None, DecisionAction.REUSE, 'existing_complete_score_reuse'),
        (None, TARGET_MODEL, DecisionAction.REUSE, 'existing_complete_score_reuse'),
    ],
)
def test_complete_reuse_is_independent_of_execution_model(
    stored_model, target_model, expected_action, expected_reason
):
    job = {'id': 'test', 'title': 'C++ Developer'}
    profile = {'skills': ['C++']}
    previous = AnalysisRecord(
        job_id='test',
        analysis_status=AnalysisStatus.COMPLETE,
        match_score=70,
        analysis_version=ANALYSIS_VERSION,
        prompt_version=PROMPT_VERSION,
        content_fingerprint=content_fingerprint(job),
        profile_fingerprint=profile_fingerprint(profile),
        model=stored_model,
    )

    decision = decide_reuse(previous, job, profile, model=target_model)

    assert decision.action == expected_action
    assert decision.reason == expected_reason
