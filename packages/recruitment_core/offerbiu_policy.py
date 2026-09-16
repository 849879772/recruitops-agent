"""User-selected cohort policy, distinct from official-page evidence."""
from copy import deepcopy
from collections.abc import Mapping


def is_offerbiu_source(value):
    return any(str(value.get(key) or '').lower() in {'offerbiu', 'offerbiu_snapshot'}
               for key in ('source', 'discovery_source'))


def apply_offerbiu_cohort(job: Mapping) -> dict:
    result = deepcopy(dict(job))
    result.setdefault('original_cohort_evidence', {
        key: deepcopy(job.get(key)) for key in
        ('cohort', 'cohort_status', 'cohort_source', 'cohort_evidence')
    })
    result.update(cohort=2027, cohort_status='confirmed',
                  cohort_source='offerbiu_user_policy',
                  cohort_evidence='User policy: all OfferBiu-sourced companies are assigned to 2027.',
                  discovery_source='offerbiu', cohort_policy='offerbiu_force_2027')
    return result
