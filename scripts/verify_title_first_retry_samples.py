"""Bounded live detail diagnostics; no catalog writes or scoring."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from threading import Lock
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.pipeline.isolation import fetch_job_detail_result_isolated
from packages.recruitment_core.jd_capture import assess_jd_capture


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def prepare(source, output):
    paths = [source / f'{name}.jsonl' for name in
             ('existing-repairs', 'new-failures', 'missing-jd')]
    groups = [[json.loads(line) for line in p.read_text(encoding='utf-8').splitlines() if line]
              for p in paths]
    selected = [('existing_empty', row) for row in groups[0]]
    reasons = defaultdict(list)
    for row in groups[1]:
        reasons[row['capture_failure_reason']].append(row)
    for reason, rows in sorted(reasons.items()):
        companies = set()
        for row in rows:
            if row['company_id'] in companies:
                continue
            selected.append((reason, row))
            companies.add(row['company_id'])
            if len(companies) == 2:
                break
    domains = set()
    for row in groups[2]:
        domain = urlsplit(row['detail_url']).netloc
        if domain in domains:
            continue
        selected.append(('missing_detail', row))
        domains.add(domain)
        if len(domains) == 8:
            break
    if len(selected) > 40:
        raise ValueError('Pilot exceeds 40-job bound')
    samples = []
    for index, (stratum, row) in enumerate(selected):
        observations = row['observations']
        observation = next((o for o in observations if
                            (o['raw_job'].get('jd_url') or o['raw_job'].get('detail_url'))
                            == row['detail_url']), observations[0])
        job = dict(observation['raw_job'])
        job.update(company=row['company_name'], title=row['title'],
                   company_id=row['company_id'], detail_url=row['detail_url'],
                   jd_url=row['detail_url'], careers_url=observation['source_url'],
                   detail_capture_policy='title_first_v2')
        old_ids = row.get('existing_job_ids', [])
        if old_ids:
            job['id'] = old_ids[0]
        samples.append({'index': index, 'stratum': stratum, 'job': job,
                        'existing_job_ids': old_ids, 'historical_row': row})
    manifest = {'schema': 1, 'read_only': True, 'samples': samples,
                'inputs': [{'path': str(p.resolve()), 'sha256': digest(p)} for p in paths]}
    save(output / 'selection.json', manifest)
    return manifest


def run_sample(sample, output, locks, timeout, fetch=fetch_job_detail_result_isolated):
    path = output / f"result-{sample['index']:03}.json"
    if path.exists():
        return read(path)
    from packages.recruitment_core.offerbiu_policy import apply_offerbiu_cohort

    job = apply_offerbiu_cohort(sample['job'])
    domain = urlsplit(job['detail_url']).netloc
    with locks[domain]:
        started = time.monotonic()
        try:
            response = fetch(job, timeout_seconds=timeout)
        except Exception as exc:
            response = {'status': 'exception', 'error_type': type(exc).__name__,
                        'error': str(exc)[:1200], 'detail': ''}
        assessment = assess_jd_capture({**job, 'jd_raw': response.get('detail'),
                                       'capture_evidence': response.get('capture_evidence') or {}})
        result = {'index': sample['index'], 'stratum': sample['stratum'],
                  'company': job['company'], 'title': job['title'],
                  'detail_url': job['detail_url'], 'existing_job_ids': sample['existing_job_ids'],
                  'job': job, 'response': response, 'capture_complete': assessment.complete,
                  'blocked_before_fetch': response.get('status') == 'cohort_ineligible',
                  'assessment_reason': assessment.reason_code,
                  'failure_reason': None if assessment.complete else
                      (response.get('error_type') or response.get('status') or assessment.reason_code),
                  'elapsed_seconds': round(time.monotonic() - started, 2),
                  'tested_at': datetime.now(timezone.utc).isoformat(),
                  'db_writes': 0, 'model_calls': 0, 'full_company_validation': False}
        save(path, result)
        print(json.dumps({k: result[k] for k in ('index', 'company', 'capture_complete', 'failure_reason')},
                         ensure_ascii=False), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--retry-blocked-from', type=Path)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / '.data/evals') or output == args.source.resolve():
        raise ValueError('Use an independent evaluation directory')
    if not 1 <= args.workers <= 4 or not 1 <= args.timeout <= 60:
        raise ValueError('Bounds: workers 1..4, timeout 1..60')
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / 'selection.json'
    if manifest_path.exists():
        manifest = read(manifest_path)
    elif args.retry_blocked_from:
        previous = args.retry_blocked_from.resolve()
        if previous == output:
            raise ValueError('Retest requires a new directory')
        old = read(previous / 'selection.json')
        selected = []
        inputs = list(old['inputs'])
        inputs.append({'path': str(previous / 'selection.json'),
                       'sha256': digest(previous / 'selection.json')})
        for sample in old['samples']:
            result_path = previous / f"result-{sample['index']:03}.json"
            result = read(result_path)
            inputs.append({'path': str(result_path), 'sha256': digest(result_path)})
            if result['response'].get('status') == 'cohort_ineligible':
                sample['job']['detail_capture_policy'] = 'title_first_v2'
                selected.append(sample)
        if not 1 <= len(selected) <= 40:
            raise ValueError('Blocked retest must select 1..40 records')
        manifest = {'schema': 2, 'read_only': True, 'samples': selected,
                    'inputs': inputs, 'retry_blocked_from': str(previous)}
        save(manifest_path, manifest)
    else:
        manifest = prepare(args.source, output)
    for item in manifest['inputs']:
        if digest(Path(item['path'])) != item['sha256']:
            raise ValueError('Frozen input changed')
    if args.prepare_only:
        print(json.dumps({'selected': len(manifest['samples']),
                          'strata': dict(Counter(s['stratum'] for s in manifest['samples']))}))
        return
    locks = {urlsplit(s['job']['detail_url']).netloc: Lock() for s in manifest['samples']}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_sample, sample, output, locks, args.timeout)
                   for sample in manifest['samples']]
        results = [future.result() for future in as_completed(futures)]
    strata = {}
    for key in sorted({r['stratum'] for r in results}):
        rows = [r for r in results if r['stratum'] == key]
        strata[key] = {'tested': len(rows), 'complete': sum(r['capture_complete'] for r in rows)}
    summary = {'selected': len(manifest['samples']), 'tested': len(results),
               'complete': sum(r['capture_complete'] for r in results), 'strata': strata,
               'blocked_before_fetch': sum(r['response'].get('status') == 'cohort_ineligible' for r in results),
               'failures': dict(Counter(r['failure_reason'] for r in results if not r['capture_complete'])),
               'db_writes': 0, 'model_calls': 0, 'full_company_validation': False,
               'input_hashes_unchanged': all(digest(Path(i['path'])) == i['sha256'] for i in manifest['inputs'])}
    save(output / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
