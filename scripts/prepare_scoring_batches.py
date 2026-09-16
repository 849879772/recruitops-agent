"""Prepare bounded review inputs without calling a model or writing business data."""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.matching.models import DecisionAction
from packages.matching.rules import content_fingerprint, profile_fingerprint
from packages.matching.service import decide_reuse

JOB_FIELDS = (
    'id', 'company', 'company_id', 'title', 'city', 'detail_url', 'jd_raw',
    'cohort', 'cohort_status', 'batch', 'source', 'source_ref',
    'capture_status', 'capture_failure_reason', 'capture_evidence',
    'content_fingerprint',
)


def prepare(manifest: dict, *, max_jobs: int, max_characters: int,
            assigned_ids: set[str] | None = None,
            target_model: str | None = None) -> tuple[list[dict], list[dict]]:
    if max_jobs < 1 or max_characters < 1:
        raise ValueError('Batch limits must be positive')
    profile = manifest['profile']
    if profile_fingerprint(profile) != manifest['profile_fingerprint']:
        raise ValueError('Profile fingerprint mismatch')
    eligible = []
    excluded = []
    seen = set()
    for job in manifest['jobs']:
        if job['id'] in seen:
            raise ValueError('Duplicate job ID in frozen input')
        seen.add(job['id'])
        if content_fingerprint(job) != job['content_fingerprint']:
            raise ValueError(f"Job fingerprint mismatch: {job['id']}")
        if job['id'] in (assigned_ids or set()):
            excluded.append({'job_id': job['id'], 'company': job.get('company'),
                             'title': job['title'], 'status': 'already_assigned',
                             'reasons': ['Assigned to a separate review batch'], 'evidence': []})
            continue
        if decide_reuse(
            job.get('previous_analysis'), job, profile, model=target_model,
        ).action is DecisionAction.REUSE:
            excluded.append({'job_id': job['id'], 'company': job.get('company'),
                             'title': job['title'], 'status': 'already_complete_reusable',
                             'reasons': ['Complete analysis matches the current job, profile, versions, and target model'],
                             'evidence': []})
            continue
        eligible.append({key: job.get(key) for key in JOB_FIELDS})
    batches = []
    batch = []
    size = 0
    for job in eligible:
        item_size = len(json.dumps(job, ensure_ascii=False))
        if batch and (len(batch) >= max_jobs or size + item_size > max_characters):
            batches.append(batch)
            batch, size = [], 0
        batch.append(job)
        size += item_size
    if batch:
        batches.append(batch)
    return [{'run_id': manifest['run_id'],
             'review_mode': manifest.get('review_mode', 'score_only'),
             'review_model': manifest.get('review_model', target_model),
             'profile': profile,
             'profile_fingerprint': manifest['profile_fingerprint'], 'jobs': rows}
            for rows in batches], excluded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--max-jobs', type=int, default=40)
    parser.add_argument('--max-characters', type=int, default=100_000)
    parser.add_argument('--assigned-input', type=Path, action='append', default=[])
    parser.add_argument('--target-model', default=None)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite an existing queue')
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    assigned_ids = set()
    for path in args.assigned_input:
        assigned = json.loads(path.read_text(encoding='utf-8'))
        if assigned.get('run_id') != manifest['run_id']:
            raise ValueError('Assigned input run ID mismatch')
        assigned_ids.update(job['id'] for job in assigned['jobs'])
    batches, excluded = prepare(manifest, max_jobs=args.max_jobs,
                                 max_characters=args.max_characters, assigned_ids=assigned_ids,
                                 target_model=args.target_model)
    args.output.mkdir(parents=True)
    queue = []
    for index, batch in enumerate(batches, start=1):
        filename = f'batch-{index:03d}.json'
        (args.output / filename).write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding='utf-8')
        queue.append({'file': filename, 'count': len(batch['jobs']), 'status': 'pending'})
    (args.output / 'excluded.json').write_text(json.dumps(excluded, ensure_ascii=False, indent=2), encoding='utf-8')
    ledger = {'run_id': manifest['run_id'], 'input_sha256': sha256(raw).hexdigest(),
              'target_model': args.target_model,
              'total': len(manifest['jobs']), 'eligible_candidates': sum(b['count'] for b in queue),
              'excluded_by_rule': dict(Counter(e['status'] for e in excluded)), 'batches': queue}
    (args.output / 'queue.json').write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in ledger.items() if k != 'batches'} | {'batches': len(queue)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
