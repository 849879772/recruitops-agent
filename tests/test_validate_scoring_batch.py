from packages.matching.rules import content_fingerprint, profile_fingerprint
from scripts.validate_scoring_batch import validate_batch


def batch():
    job = {'id': 'a', 'title': 'Developer', 'jd_raw': 'Incomplete'}
    job['content_fingerprint'] = content_fingerprint(job)
    return {'run_id': 'test', 'profile': {}, 'profile_fingerprint': profile_fingerprint({}), 'jobs': [job]}


def result(reviews):
    return {'run_id': 'test', 'model': 'gpt-5.6-luna', 'reviews': reviews}


def test_missing_or_duplicate_reviews_do_not_pass():
    assert validate_batch(batch(), result([]))['errors'][0]['reason'] == 'missing_review'
    review = {'job_id': 'a', 'decision': 'exclude', 'reason': '排除：岗位核心为销售运营，不属于目标研发方向。'}
    assert not validate_batch(batch(), result([review, review]))['valid']


def test_defer_is_rejected_from_scoring_queue_and_model_provenance_is_bound():
    review = {'job_id': 'a', 'decision': 'defer', 'reason': 'Incomplete source'}
    report = validate_batch(batch(), result([review]))
    assert not report['valid']
    assert report['errors'][0]['reason'] == 'decision_not_allowed_in_score_only'
    other_model = result([review]) | {'model': 'deepseek'}
    assert any(e['reason'] == 'run_or_model_mismatch' for e in validate_batch(batch(), other_model)['errors'])


def test_exclude_is_rejected_from_score_only_queue():
    review = {'job_id': 'a', 'decision': 'exclude', 'reason': '方向不符'}
    assert validate_batch(batch(), result([review]))['errors'][0]['reason'] == (
        'decision_not_allowed_in_score_only'
    )


def test_changed_source_is_rejected():
    source = batch()
    source['jobs'][0]['jd_raw'] = 'Changed'
    reviews = result([{'job_id': 'a', 'decision': 'exclude', 'reason': '排除：岗位核心为销售运营，不属于目标研发方向。'}])
    assert validate_batch(source, reviews)['errors'][0]['reason'] == 'content_fingerprint_mismatch'
