"""Freeze the local catalog and profile for reproducible, offline model review."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import Settings
from packages.matching.models import DecisionAction
from packages.matching.service import decide_reuse
from packages.matching.title_policy import screen_title_job
from packages.matching.rules import content_fingerprint, profile_fingerprint
from packages.recruitment_core.jd_capture import assess_jd_capture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--job-id', action='append', default=[])
    parser.add_argument('--review-model', default=None)
    parser.add_argument(
        '--pending-only',
        action='store_true',
        help='freeze only title-eligible, capture-complete jobs without a completed score',
    )
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite a frozen input')
    if not args.backup.is_file() or not args.backup.stat().st_size:
        raise SystemExit('A non-empty database backup is required')
    if len(set(args.job_id)) != len(args.job_id):
        raise SystemExit('Duplicate requested job ID')
    settings = Settings()
    loaded = yaml.safe_load(settings.candidate_profile_config.read_text(encoding='utf-8'))
    profile = loaded.get('profile', loaded)
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        query = '''
            SELECT j.*, c.name AS company, row_to_json(a) AS previous_analysis
            FROM job_snapshots j
            LEFT JOIN company_snapshots c ON c.id = j.company_id
            LEFT JOIN job_analysis_snapshots a ON a.job_id = j.id
        '''
        parameters = {}
        if args.job_id:
            query += ' WHERE j.id = ANY(:job_ids)'
            parameters['job_ids'] = args.job_id
        jobs = [dict(row) for row in conn.execute(text(query + ' ORDER BY j.id'), parameters).mappings()]
        applications = [dict(row) for row in conn.execute(text(
            'SELECT * FROM application_snapshots ORDER BY id'
        )).mappings()]
        conn.rollback()
    engine.dispose()
    if args.job_id and {job['id'] for job in jobs} != set(args.job_id):
        raise SystemExit('Requested job missing from current catalog')
    if args.pending_only:
        jobs = [
            job
            for job in jobs
            if screen_title_job(job, profile).eligible
            and assess_jd_capture(job).complete
            and decide_reuse(
                job.get('previous_analysis'),
                job,
                profile,
                model=args.review_model or settings.llm_model,
            ).action is not DecisionAction.REUSE
        ]
    for job in jobs:
        job['content_fingerprint'] = content_fingerprint(job)
    applications_json = json.dumps(applications, ensure_ascii=False, sort_keys=True, default=str)
    payload = {
        'run_id': args.run_id,
        'review_mode': 'score_only',
        'review_model': args.review_model or settings.llm_model,
        'frozen_at': datetime.now(timezone.utc).isoformat(),
        'profile': profile,
        'profile_fingerprint': profile_fingerprint(profile),
        'application_count': len(applications),
        'application_sha256': sha256(applications_json.encode('utf-8')).hexdigest(),
        'backup': {'path': str(args.backup.resolve()),
                   'sha256': sha256(args.backup.read_bytes()).hexdigest()},
        'jobs': jobs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({
        'manifest': str(args.output.resolve()), 'run_id': args.run_id, 'jobs': len(jobs),
        'applications': len(applications), 'backup_bytes': args.backup.stat().st_size,
        'previous_statuses': dict(Counter((j['previous_analysis'] or {}).get('analysis_status', 'missing') for j in jobs)),
        'database_writes': 0,
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
