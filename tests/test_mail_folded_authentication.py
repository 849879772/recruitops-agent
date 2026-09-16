import pytest
from packages.recruitment_mail.connectors import _authentication_identity_domain


@pytest.mark.parametrize("source", [
    " smtp.mail=recruit\r\n ment@dji.com",
    " smtp.mail=<recruit ment@dji.com> (receiver annotation)",
    " smtp.mail=recruit ment@dji.com smtp.helo=unrelated.example",
    " smtp.mailfrom=recruitment@dji. com;",
    " header.i=@dji.com",
])
def test_folded_mailbox_identity_keeps_only_its_domain(source):
    assert _authentication_identity_domain(source) == "dji.com"


@pytest.mark.parametrize("source", [
    " smtp.mail=recruit (annotation)",
    " smtp.mail=recruit smtp.helo=dji.com",
    " smtp.mail=recruit (contact other@dji.com)",
    " smtp.helo=dji.com",
])
def test_incomplete_mailbox_or_helo_is_not_an_authenticated_domain(source):
    assert _authentication_identity_domain(source) is None
