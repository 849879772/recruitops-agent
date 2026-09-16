import pytest

from packages.domain.urls import normalize_http_page_url


def test_normalize_http_page_url_preserves_spa_route_and_drops_queries() -> None:
    assert normalize_http_page_url(
        "https://app.mokahr.com/campus/acme/1?session=secret"
        "#/candidateHome/applications?tab=current"
    ) == "https://app.mokahr.com/campus/acme/1#/candidateHome/applications"


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "https://user:secret@example.com/applications",
        "https://example.com:invalid/applications",
    ],
)
def test_normalize_http_page_url_rejects_unsafe_authorities(value: str) -> None:
    assert normalize_http_page_url(value) is None


def test_normalize_http_page_url_drops_non_route_fragments() -> None:
    assert normalize_http_page_url("https://example.com/jobs#marketing") == (
        "https://example.com/jobs"
    )


def test_normalize_http_page_url_drops_default_port_like_browser_url() -> None:
    assert normalize_http_page_url("https://EXAMPLE.com:443/applications") == (
        "https://example.com/applications"
    )
