"""Latest-only receipts, not a second stage-history or page-evidence store."""
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

from sqlalchemy import select, update

from packages.domain.urls import normalize_http_page_url
from .models import ApplicationSnapshot

IDENTITY_REASONS = frozenset({
    'target_record_not_matched', 'target_record_ambiguous', 'target_card_not_unique', 'model_identity_mismatch',
})

_NAVIGATION_REASONS = frozenset({
    'current_origin_changed', 'same_origin_navigation', 'initial_origin_mismatch',
    'loaded_origin_mismatch', 'result_origin_mismatch', 'renderer_gone',
    'official_sso_hop_limit', 'official_sso_reobservation_limit',
    'returned_to_home_without_application_records',
    'application_record_entry_not_entered', 'application_record_home_redirect',
    'application_record_entry_followed',
} | {f'{event}_{scope}' for event in (
    'will_navigate', 'will_redirect', 'did_start_navigation', 'did_navigate_in_page',
) for scope in ('same_origin', 'cross_origin', 'official_sso')})
_NAVIGATION_ROUTE = re.compile(
    r'^(?:(?:app|application|applications|application_center|status|record|records|recruit|recruitment|campus|candidate|candidatehome|user|center|account|accounts|auth|sso|saml|oauth|oauth2|authorize|callback|redirect|redirected|login|signin|sign|in|home|index|position|positions|job|jobs|portal|error|404|html|htm|aspx|php|pb|www|[._-]))+$',
    re.IGNORECASE,
)


def _navigation_url(value):
    """Re-sanitize legacy diagnostics too: URLs are not browser navigation grants."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    identity = normalize_http_page_url(value)
    if not identity:
        return None
    parsed = urlsplit(identity)
    def route(path):
        return '/'.join(segment if not segment or _NAVIGATION_ROUTE.fullmatch(segment)
                        else '[redacted]' for segment in path.split('/'))
    fragment = '#' + route(parsed.fragment) if parsed.fragment else ''
    return f'{parsed.scheme}://{parsed.netloc}{route(parsed.path)}{fragment}'[:1024]


def safe_review_navigation_diagnostics(value):
    """Bounded enum/count/route receipt; never retain auth forms or page snippets."""
    if not isinstance(value, dict):
        return {}
    safe = {}
    for key, allowed in (
        ('reason', _NAVIGATION_REASONS),
        ('phase', {'initial_load', 'observation', 'navigation_recovery', 'vision_capture'}),
        ('restriction', {'https_downgrade', 'unapproved_origin', 'credential_url', 'invalid_url'}),
    ):
        if isinstance(value.get(key), str) and value[key] in allowed:
            safe[key] = value[key]
    for key in ('sameOrigin', 'ssoCandidate'):
        if type(value.get(key)) is bool:
            safe[key] = value[key]
    for key in ('requestedUrl', 'finalUrl', 'attemptedUrl'):
        url = _navigation_url(value.get(key))
        if url:
            safe[key] = url
    if type(value.get('reobservationCount')) is int and 0 <= value['reobservationCount'] <= 1:
        safe['reobservationCount'] = value['reobservationCount']
    auth = value.get('authNavigation')
    if isinstance(auth, dict) and isinstance(auth.get('provider'), str) and auth['provider'] in {'alibaba', 'huawei'}:
        projected = {'provider': auth['provider']}
        if type(auth.get('hops')) is int and 0 <= auth['hops'] <= 9:
            projected['hops'] = auth['hops']
        if type(auth.get('returnedToRecruitment')) is bool:
            projected['returnedToRecruitment'] = auth['returnedToRecruitment']
        safe['authNavigation'] = projected
    wait = value.get('authWait')
    if isinstance(wait, dict) and isinstance(wait.get('outcome'), str) and wait['outcome'] in {'returned', 'timeout', 'cancelled', 'navigation_denied'}:
        projected = {'outcome': wait['outcome']}
        for key, maximum in (('elapsedMs', 120_000), ('budgetMs', 15_000), ('progressCount', 10_000)):
            if type(wait.get(key)) is int and 0 <= wait[key] <= maximum:
                projected[key] = wait[key]
        safe['authWait'] = projected
    return safe


def safe_review_receipt_diagnostics(row):
    diagnostics = row.get('diagnostics')
    observation = row.get('observation')
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    if isinstance(observation, dict):
        observed = observation.get('diagnostics')
        if not diagnostics.get('navigation_diagnostics') and isinstance(observed, dict):
            diagnostics = observed
    navigation = safe_review_navigation_diagnostics(diagnostics.get('navigation_diagnostics'))
    return {'navigation_diagnostics': navigation} if navigation else {}


def review_time(value, fallback):
    try:
        when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        when = fallback
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def save_latest_reviews(session, rows, *, checked_at, run_id=None):
    """Caller owns write permission and transaction. Older replays cannot win."""
    incoming = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get('application_id') or not row.get('state'):
            continue
        receipt = {key: str(row[key])[:200] for key in (
            'state', 'reason', 'observed_status', 'observed_label', 'presentation_state',
            'saved_stage', 'operation_id', 'model_disposition', 'vision_disposition',
        ) if row.get(key) is not None}
        receipt['checked_at'] = review_time(row.get('checked_at'), checked_at).isoformat()
        receipt['wrote'] = bool(row.get('wrote'))
        diagnostics = safe_review_receipt_diagnostics(row)
        if diagnostics:
            receipt['diagnostics'] = diagnostics
        if run_id:
            receipt['run_id'] = str(run_id)[:128]
        incoming[str(row['application_id'])] = receipt
    if not incoming:
        return
    # Serialize overlapping reviews; explicit updated_at preserves business ordering.
    apps = session.scalars(select(ApplicationSnapshot).where(
        ApplicationSnapshot.id.in_(incoming)).order_by(ApplicationSnapshot.id).with_for_update())
    for app in apps:
        receipt = incoming[app.id]
        previous = app.last_review or {}
        if previous.get('checked_at') and review_time(previous['checked_at'], checked_at) >= review_time(receipt['checked_at'], checked_at):
            continue
        session.execute(update(ApplicationSnapshot).where(ApplicationSnapshot.id == app.id).values(
            last_review=receipt, updated_at=app.updated_at))
