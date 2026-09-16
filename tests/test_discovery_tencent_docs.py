from __future__ import annotations

import base64
import json
import zlib

from packages.discovery import (
    TencentDocsSmartSheetConnector,
    canonical_company_name,
    parse_rows,
)


def _jsonp(payload) -> str:
    client_vars = {
        "clientVars": {
            "collab_client_vars": {
                "initialAttributedText": {
                    "text": [{
                        "smartsheet": base64.urlsafe_b64encode(
                            zlib.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                        ).decode("ascii").rstrip("=")
                    }]
                }
            }
        }
    }
    return "clientVarsCallback(" + json.dumps(client_vars, ensure_ascii=False) + ");"


def _row(name: str, tags: list[str], link: str):
    return {
        "row": {
            "k1": {
                "f_company": {"k1": [{"k2": name}]},
                "f_type": {"k9": tags},
                "f_link": {"k8": [{"k3": link}]},
            }
        }
    }


def _first_page():
    return [
        {
            "f_company": {"k30": "公司名称"},
            "f_type": {
                "k30": "招聘类型",
                "k9": {"k3": [
                    {"k1": "formal", "k2": "27届秋招"},
                    {"k1": "early", "k2": "27届秋招提前批"},
                ]},
            },
            "f_link": {"k30": "投递链接"},
        },
        _row("DJI大疆", ["formal", "early"], "https://example.com/dji"),
        _row("不应保留", ["early"], "https://example.com/no"),
    ]


class _Response:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, first: str, later: str, empty: str):
        self.first = first
        self.later = later
        self.empty = empty
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if url.startswith("https://docs.qq.com/smartsheet/"):
            return _Response(
                '<link rel="preload" as="script" href="/dop-api/opendoc?tab=1">'
            )
        if "startrow=60" in url:
            return _Response(self.later)
        if "startrow=" in url:
            return _Response(self.empty)
        return _Response(self.first)


def test_tencent_parser_keeps_exact_tag_and_supports_later_pages() -> None:
    later = [_row("第二公司", ["formal"], "https://second.example/jobs")]
    rows = parse_rows([_first_page(), later])

    assert [row.source_name for row in rows] == ["DJI大疆", "第二公司"]
    assert rows[0].canonical_name == "大疆"
    assert rows[0].metadata["mixed_tags"] == ("27届秋招提前批",)
    assert rows[0].source_identity is not None
    assert canonical_company_name("阿里-淘宝闪购") == "淘天"


def test_tencent_connector_uses_get_only_jsonp_pagination() -> None:
    first = _jsonp(_first_page())
    later = _jsonp([_row("第二公司", ["formal"], "https://second.example/jobs")])
    empty = _jsonp([_row("DJI大疆", ["formal"], "https://example.com/dji")])
    session = _Session(first, later, empty)
    connector = TencentDocsSmartSheetConnector(session=session, max_pages=4)

    result = connector.sync()

    assert [lead.source_name for lead in result.leads] == ["DJI大疆", "第二公司"]
    assert result.pages_fetched == 4
    assert result.rows_seen == 2
    assert all("startrow=60" not in url or "endrow=120" in url for url, _ in session.calls)
    assert all("Referer" in kwargs.get("headers", {}) for url, kwargs in session.calls[1:])
    assert all("GET" not in kwargs for _, kwargs in session.calls)
    assert result.metadata["read_only"] is True

