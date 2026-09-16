"""Apply explicitly reviewed JD repairs with optimistic content checks."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy import select

from packages.matching.rules import content_fingerprint, profile_fingerprint, screen_job
from packages.recruitment_core.jd_capture import assess_jd_capture
from packages.storage import CompanySnapshot, JobAnalysisSnapshot, JobSnapshot, Storage
from .jd_repair import content_sha256, validate_candidate


def import_jd_repairs(storage: Storage, report: dict, profile: dict, approved_ids: list[str],
                      *, apply: bool = False, backup: dict | None = None) -> dict:
    if not approved_ids or len(set(approved_ids)) != len(approved_ids):
        raise ValueError('Provide a non-empty, unique approved job ID list')
    records = report.get('results', [])
    by_id = {}
    for item in records:
        if item.get('job_id') in by_id:
            raise ValueError('Duplicate repair job ID')
        by_id[item.get('job_id')] = item
    if set(approved_ids) - by_id.keys():
        raise ValueError('Approved job missing from repair report')
    if apply:
        if not backup:
            raise ValueError('Verified database backup required')
        archive = Path(backup['path'])
        if not archive.is_file() or not archive.stat().st_size or sha256(archive.read_bytes()).hexdigest() != backup['sha256']:
            raise ValueError('Database backup checksum mismatch')
    result = {'planned': 0, 'written': 0, 'reused': 0, 'dry_run': not apply, 'jobs': []}
    with storage.transaction(write=apply) as session:
        for job_id in approved_ids:
            item = by_id[job_id]
            row = session.execute(select(JobSnapshot).where(JobSnapshot.id == job_id).with_for_update()).scalar_one_or_none()
            if row is None:
                raise ValueError(f'Job missing: {job_id}')
            if row.cohort != 2027 or row.cohort_status != 'confirmed':
                raise ValueError(f'Job outside approved recruitment scope: {job_id}')
            company = session.get(CompanySnapshot, row.company_id)
            if company is None or any((
                row.company_id != item.get('company_id'), company.name != item.get('company'),
                row.title != item.get('title'), row.detail_url != item.get('detail_url'),
            )):
                raise ValueError(f'Job identity changed: {job_id}')
            candidate = item.get('candidate_jd')
            if not isinstance(candidate, str) or not candidate or content_sha256(candidate) != item.get('candidate_sha256'):
                raise ValueError(f'Candidate checksum mismatch: {job_id}')
            observation = item.get('validation', {})
            if observation.get('detail_url') != row.detail_url:
                raise ValueError(f'Observed detail URL does not match job: {job_id}')
            payload = {field: getattr(row, field) for field in (
                'id', 'company_id', 'title', 'city', 'detail_url', 'jd_raw', 'cohort',
                'cohort_status', 'batch', 'source_platform', 'source_tenant', 'recruitment_campaign_id',
            )}
            payload.update(company=company.name, company_campus_url=company.campus_url)
            verified = validate_candidate(payload, {**observation, 'detail': candidate})
            payload['capture_evidence'] = observation.get('capture_evidence') or {}
            quality = assess_jd_capture({**payload, 'jd_raw': candidate})
            if not verified['passed'] or not quality.complete:
                raise ValueError(f'Candidate not valid: {job_id}: {verified["failure_reasons"]}, {quality.reason_code}')
            if content_sha256(row.jd_raw) == item['candidate_sha256']:
                result['reused'] += 1
                result['jobs'].append({'job_id': job_id, 'status': 'reused'})
                continue
            if content_sha256(row.jd_raw) != item.get('original_sha256'):
                raise ValueError(f'Original JD changed: {job_id}')
            screening = screen_job({**payload, 'jd_raw': candidate}, profile)
            result['planned'] += 1
            result['jobs'].append({'job_id': job_id, 'status': screening.analysis_status.value})
            if not apply:
                continue
            now = datetime.now(timezone.utc)
            row.jd_raw = candidate
            row.capture_evidence = payload['capture_evidence']
            row.match_score = None
            row.updated_at = now
            analysis = session.get(JobAnalysisSnapshot, job_id)
            if analysis is None:
                analysis = JobAnalysisSnapshot(job_id=job_id, source=row.source, created_at=now)
                session.add(analysis)
            analysis.match_score = None
            analysis.advantages = json.dumps([], ensure_ascii=False)
            analysis.gaps = json.dumps([], ensure_ascii=False)
            analysis.summary = 'JD 已补全，等待重新匹配评分' if screening.eligible else 'JD 已补全，当前筛选未通过'
            analysis.recommendation = '未评估'
            analysis.score_breakdown = {}
            analysis.evidence = []
            analysis.evidence_level = None
            analysis.analysis_status = screening.analysis_status.value
            analysis.filter_reasons = screening.reasons
            analysis.matched_directions = [d.value for d in screening.matched_directions]
            analysis.primary_match_direction = screening.primary_match_direction.value if screening.primary_match_direction else None
            analysis.content_fingerprint = content_fingerprint({**payload, 'jd_raw': candidate})
            analysis.profile_fingerprint = profile_fingerprint(profile)
            analysis.model = None
            analysis.input_tokens = None
            analysis.output_tokens = None
            analysis.refusal_reason = None
            analysis.error_code = None
            analysis.analyzed_at = now
            analysis.updated_at = now
            analysis.source_ref = f'{row.source_ref or job_id}:analysis'
            result['written'] += 1
    return result
