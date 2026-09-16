import pytest
from packages.recruitment_mail.authentication import has_aligned_authentication


def metadata():
    return {"sender_domain": "dji.com", "return_path_domain": "dji.com", "authentication_results": [
        {"authserv_id": "trusted-receiver", "method": "spf", "result": "pass", "aligned": True, "identity_domain": "dji.com"}]}


def test_strict_aligned_spf_is_accepted_without_fabricating_dkim():
    value = metadata()
    assert has_aligned_authentication(value)
    assert value["authentication_results"][0]["method"] == "spf"


@pytest.mark.parametrize("field,value", [("result", "fail"), ("aligned", False), ("authserv_id", ""), ("identity_domain", "other.com"), ("method", "arc")])
def test_unverified_spf_cannot_authorize(field, value):
    data = metadata()
    data["authentication_results"][0][field] = value
    assert not has_aligned_authentication(data)


@pytest.mark.parametrize("field", ["sender_domain", "return_path_domain"])
def test_spf_requires_exact_envelope_and_sender_alignment(field):
    data = metadata()
    data[field] = "sub.dji.com"
    assert not has_aligned_authentication(data)
    data[field] = None
    assert not has_aligned_authentication(data)
