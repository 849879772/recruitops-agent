"""Validate a complete offline review batch without network or database access."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.matching.review_import import (
    EXPECTED_MODEL, _validate_evidence, _validate_payload,
)
from packages.matching.rules import content_fingerprint, profile_fingerprint


def validate_batch(batch: dict, results: dict) -> dict:
    errors = []
    jobs = {job['id']: job for job in batch['jobs']}
    if len(jobs) != len(batch['jobs']):
        errors.append({'reason': 'duplicate_input_id'})
    expected_model = batch.get('review_model') or EXPECTED_MODEL
    if results.get('run_id') != batch['run_id'] or results.get('model') != expected_model:
        errors.append({'reason': 'run_or_model_mismatch'})
    if profile_fingerprint(batch['profile']) != batch['profile_fingerprint']:
        errors.append({'reason': 'profile_fingerprint_mismatch'})
    reviews = results.get('reviews', [])
    counts = Counter(review.get('job_id') for review in reviews)
    for job_id in sorted(jobs.keys() - counts.keys()):
        errors.append({'job_id': job_id, 'reason': 'missing_review'})
    for job_id, count in counts.items():
        if job_id not in jobs or count != 1:
            errors.append({'job_id': job_id, 'reason': 'unknown_or_duplicate_review'})
    for review in reviews:
        job_id = review.get('job_id')
        job = jobs.get(job_id)
        if not job:
            continue
        reason = None
        if content_fingerprint(job) != job.get('content_fingerprint'):
            reason = 'content_fingerprint_mismatch'
        elif review.get('decision') == 'score':
            payload = _validate_payload(review.get('analysis', {}))
            if isinstance(payload, str):
                reason = payload
            else:
                reason = _validate_evidence(payload, jd_raw=job['jd_raw'], profile=batch['profile'])
        else:
            reason = 'decision_not_allowed_in_score_only'
        if reason:
            errors.append({'job_id': job_id, 'reason': reason})
    return {'input_jobs': len(jobs), 'reviews': len(reviews), 'valid': not errors,
            'decisions': dict(Counter(review.get('decision') for review in reviews)), 'errors': errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    args = parser.parse_args()
    result = validate_batch(json.loads(args.input.read_text(encoding='utf-8')),
                            json.loads(args.results.read_text(encoding='utf-8')))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
