from copy import deepcopy
import pytest
from packages.recruitment_core.offerbiu_policy import apply_offerbiu_cohort, is_offerbiu_source
from packages.recruitment_core.job_details import fetch_full_job_description_result


@pytest.mark.parametrize('year,status', [(2026,'confirmed'), (0,'unknown'), (2027,'conflict')])
def test_forces_source_policy_without_forging_page_evidence(year, status):
    raw = {'cohort':year, 'cohort_status':status, 'cohort_source':'official page',
           'cohort_evidence':'Original page text', 'jd_raw':'', 'title':'Engineer'}
    before = deepcopy(raw)
    result = apply_offerbiu_cohort(raw)
    assert result['cohort'] == 2027 and result['cohort_status'] == 'confirmed'
    assert result['cohort_source'] == 'offerbiu_user_policy'
    assert result['original_cohort_evidence']['cohort'] == year
    assert raw == before
    assert apply_offerbiu_cohort(result) == result
    assert fetch_full_job_description_result(result).status == 'no_detail_url'


def test_detection_does_not_classify_other_sources():
    assert is_offerbiu_source({'discovery_source':'offerbiu_snapshot'})
    assert not is_offerbiu_source({'source':'oc_snapshot'})
