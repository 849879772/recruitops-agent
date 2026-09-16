import base64
import hashlib
import json
import unicodedata

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from packages.recruitment_core import job_details


class _Response:
    def __init__(self, *, text="", payload=None, url=""):
        self.text = text
        self._payload = payload
        self.url = url

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _encrypted_envelope(payload, key: str, iv: str):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    padding = 16 - len(raw) % 16
    padded = raw + bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return {"data": base64.b64encode(encrypted).decode(), "necromancer": key}


def test_moka_detail_uses_public_api_and_decrypts_complete_jd(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    key = "0123456789abcdef"
    iv = "fedcba9876543210"
    job_id = "78086983-48dd-4914-bbc1-90302918825b"
    page = (
        '<input id="init-data" value="{&quot;orgId&quot;:&quot;demo&quot;,'
        '&quot;siteId&quot;:&quot;54046&quot;,&quot;aesIv&quot;:&quot;'
        f'{iv}&quot;}}">'
    )
    payload = {
        "code": 0,
        "data": {
            "id": job_id,
            "title": "软件工程师",
            "jobDescription": "<p>岗位职责</p><p>负责软件系统开发、测试和持续优化。</p>"
            "<p>任职要求</p><p>熟悉 Python、数据结构、数据库和工程实践。</p>" * 4,
        },
    }
    calls = []
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda url, **kwargs: _Response(text=page),
    )

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return _Response(payload=_encrypted_envelope(payload, key, iv))

    monkeypatch.setattr(job_details.requests, "post", post)
    url = f"https://app.mokahr.com/campus-recruitment/demo/54046#/job/{job_id}"

    detail, status = job_details.fetch_moka_job_description_status(url)

    assert status == "complete"
    assert "岗位职责" in detail and "任职要求" in detail
    assert calls == [
        (
            "https://app.mokahr.com/api/outer/ats-apply/website/job",
            {"orgId": "demo", "jobId": job_id, "siteId": 54046, "locale": "zh-CN"},
        )
    ]


def test_moka_context_accepts_nested_org_id(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    page = (
        '<input id="init-data" value="{&quot;org&quot;:{&quot;id&quot;:&quot;ninebot&quot;},'
        '&quot;siteId&quot;:&quot;45627&quot;,&quot;aesIv&quot;:&quot;fedcba9876543210&quot;}">'
    )
    monkeypatch.setattr(job_details.requests, "get", lambda url, **kwargs: _Response(text=page))

    assert job_details._moka_site_context(
        "https://app.mokahr.com/campus-recruitment/ninebot/45627"
    ) == ("ninebot", 45627, "fedcba9876543210")


def test_moka_context_recovers_fields_from_invalid_page_json(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    page = (
        '<input id="init-data" value="{&quot;org&quot;:{&quot;id&quot;:&quot;ourpalm&quot;},'
        '&quot;description&quot;:&quot;构建&quot;研运一体&quot;生态&quot;,'
        '&quot;aesIv&quot;:&quot;fedcba9876543210&quot;,&quot;siteId&quot;:&quot;43628&quot;}">'
    )
    monkeypatch.setattr(job_details.requests, "get", lambda url, **kwargs: _Response(text=page))

    assert job_details._moka_site_context(
        "https://app.mokahr.com/campus-recruitment/ourpalm/43628"
    ) == ("ourpalm", 43628, "fedcba9876543210")


def test_moka_detail_rejects_response_for_another_job(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    key = "0123456789abcdef"
    iv = "fedcba9876543210"
    page = (
        '<input id="init-data" value="{&quot;orgId&quot;:&quot;demo&quot;,'
        '&quot;siteId&quot;:54046,&quot;aesIv&quot;:&quot;fedcba9876543210&quot;}">'
    )
    monkeypatch.setattr(job_details.requests, "get", lambda url, **kwargs: _Response(text=page))
    monkeypatch.setattr(
        job_details.requests,
        "post",
        lambda url, **kwargs: _Response(
            payload=_encrypted_envelope(
                {"data": {"id": "different-job", "jobDescription": "岗位职责"}}, key, iv
            )
        ),
    )

    detail, status = job_details.fetch_moka_job_description_status(
        "https://app.mokahr.com/campus_apply/demo/54046#/job/78086983-48dd-4914-bbc1-90302918825b"
    )

    assert detail == ""
    assert status == "fetch_failed"


def test_moka_detail_rejects_suffix_lookalike_and_plain_http(monkeypatch) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("untrusted URL must not trigger a request")

    monkeypatch.setattr(job_details.requests, "get", unexpected)
    assert job_details.fetch_moka_job_description_status(
        "https://notmokahr.com/campus-recruitment/demo/1#/job/78086983-48dd-4914-bbc1-90302918825b"
    ) == ("", "not_applicable")
    assert job_details.fetch_moka_job_description_status(
        "http://app.mokahr.com/campus-recruitment/demo/1#/job/78086983-48dd-4914-bbc1-90302918825b"
    ) == ("", "not_applicable")


def test_moka_detail_allows_adapter_verified_custom_host(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    key = "0123456789abcdef"
    iv = "fedcba9876543210"
    job_id = "78086983-48dd-4914-bbc1-90302918825b"
    page = (
        '<input id="init-data" value="{&quot;org&quot;:{&quot;id&quot;:&quot;demo&quot;},'
        '&quot;siteId&quot;:1,&quot;aesIv&quot;:&quot;fedcba9876543210&quot;}">'
    )
    monkeypatch.setattr(job_details.requests, "get", lambda url, **kwargs: _Response(text=page))
    monkeypatch.setattr(
        job_details.requests,
        "post",
        lambda url, **kwargs: _Response(payload=_encrypted_envelope({
            "data": {
                "id": job_id,
                "title": "软件工程师",
                "jobDescription": "岗位职责：" + "负责软件系统开发和测试。" * 30,
            }
        }, key, iv)),
    )

    detail, status = job_details.fetch_moka_job_description_status(
        f"https://campus.example.com/campus_apply/demo/1#/job/{job_id}",
        trusted_custom_host=True,
    )

    assert status == "complete"
    assert "岗位职责" in detail


def test_moka_detail_supports_legacy_apply_path(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    key = "0123456789abcdef"
    iv = "fedcba9876543210"
    job_id = "78086983-48dd-4914-bbc1-90302918825b"
    page = (
        '<input id="init-data" value="{&quot;orgId&quot;:&quot;demo&quot;,'
        '&quot;siteId&quot;:1,&quot;aesIv&quot;:&quot;fedcba9876543210&quot;}">'
    )
    monkeypatch.setattr(job_details.requests, "get", lambda url, **kwargs: _Response(text=page))
    monkeypatch.setattr(
        job_details.requests,
        "post",
        lambda url, **kwargs: _Response(payload=_encrypted_envelope({
            "data": {
                "id": job_id,
                "title": "软件工程师",
                "jobDescription": "岗位职责：" + "负责软件系统开发和测试。" * 30,
            }
        }, key, iv)),
    )

    detail, status = job_details.fetch_moka_job_description_status(
        f"https://app.mokahr.com/apply/demo/1#/job/{job_id}"
    )

    assert status == "complete"
    assert "岗位职责" in detail


def test_moka_full_detail_does_not_fall_back_to_browser(monkeypatch) -> None:
    monkeypatch.setattr(
        job_details,
        "fetch_moka_job_description_status",
        lambda _url: ("", "official_unavailable"),
    )
    monkeypatch.setattr(
        job_details,
        "render_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Moka must not fall back to browser rendering")
        ),
    )

    assert job_details.fetch_full_job_description({
        "title": "软件工程师",
        "jd_raw": "",
        "jd_url": "https://app.mokahr.com/campus-recruitment/demo/1#/job/78086983-48dd-4914-bbc1-90302918825b",
    }) == ""


BEISEN_REQUEST_UUID = "ae0a16c4-9838-4b20-86b6-77a9d62fac78"
BEISEN_NUMERIC_ID = 270968801
BEISEN_DETAIL_URL = (
    "https://example.zhiye.com/campus/detail?jobAdId=" + BEISEN_REQUEST_UUID
)
BEISEN_TITLE = "电商数据专员(J20359)"


def _beisen_row(*, native_job_id: str | None = None, row_id: str | None = None) -> dict:
    row = {
        "company_id": "beisen-example",
        "company": "示例公司",
        "title": BEISEN_TITLE,
        "city": "上海市",
        "detail_url": BEISEN_DETAIL_URL,
        "source_tenant": "beisen:example.zhiye.com:/campus:",
        "company_campus_url": "https://example.zhiye.com/campus/jobs",
    }
    values = (row["company_id"], row["detail_url"], row["title"], row["city"])
    encoded = "\x00".join(
        " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
        for value in values
    )
    synthetic_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    row["id"] = row_id or synthetic_id
    row["native_job_id"] = native_job_id or synthetic_id
    row["source_job_id"] = ""
    return row


def _beisen_payload(**overrides) -> dict:
    data = {
        "Id": BEISEN_REQUEST_UUID,
        "JobAdId": BEISEN_NUMERIC_ID,
        "JobAdName": BEISEN_TITLE,
        "Duty": "负责机器人控制软件开发、测试与线上问题定位。" * 8,
        "Require": "熟悉 Python、Linux 和 C++，具备良好的工程实践能力。" * 8,
    }
    data.update(overrides)
    return {"Data": data}


def test_beisen_synthetic_row_id_is_ignored_but_two_id_namespaces_are_verified(monkeypatch) -> None:
    row = _beisen_row()
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=_beisen_payload()),
    )

    result = job_details.fetch_beisen_job_description_status(
        BEISEN_DETAIL_URL,
        identity=row,
    )

    assert result[1] == "complete"
    assert result.identity_status == "matched"
    assert any(f"request_id:Id:{BEISEN_REQUEST_UUID}" == item for item in result.identity_evidence)
    assert any(f"job_ad_id:JobAdId:{BEISEN_NUMERIC_ID}" == item for item in result.identity_evidence)
    assert job_details._requested_id(row) == ""


def test_beisen_true_numeric_native_id_must_match_job_ad_namespace(monkeypatch) -> None:
    row = _beisen_row(native_job_id=str(BEISEN_NUMERIC_ID), row_id="catalog-row")
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=_beisen_payload()),
    )

    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)

    assert result[1] == "complete"


def test_beisen_true_numeric_id_conflict_remains_terminal(monkeypatch) -> None:
    row = _beisen_row(native_job_id="270968802", row_id="catalog-row")
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=_beisen_payload()),
    )

    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)

    assert result == ("", "identity_mismatch")
    assert "reason:beisen_native_job_id_conflict" in result.identity_evidence


def test_beisen_source_job_id_conflict_is_terminal_even_when_native_matches(monkeypatch) -> None:
    row = _beisen_row(native_job_id=str(BEISEN_NUMERIC_ID), row_id="catalog-row")
    row["source_job_id"] = "wrong-source-id"
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=_beisen_payload()),
    )

    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)

    assert result[1] == "identity_mismatch"
    assert "reason:beisen_source_job_id_conflict" in result.identity_evidence


def test_beisen_synthetic_native_does_not_hide_source_job_id_conflict(monkeypatch) -> None:
    row = _beisen_row()
    row["source_job_id"] = "wrong-source-id"
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=_beisen_payload()),
    )

    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)

    assert result[1] == "identity_mismatch"
    assert job_details._requested_id(row) == row["source_job_id"]
    assert "reason:beisen_source_job_id_conflict" in result.identity_evidence


def test_beisen_non_synthetic_64hex_native_id_is_not_ignored(monkeypatch) -> None:
    fake_hash = "f" * 64
    row = _beisen_row(native_job_id=fake_hash, row_id=fake_hash)
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=_beisen_payload()),
    )

    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)

    assert result[1] == "identity_mismatch"
    assert job_details._requested_id(row) == fake_hash


def test_beisen_request_uuid_and_job_ad_alias_conflicts_are_rejected(monkeypatch) -> None:
    row = _beisen_row()
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=_beisen_payload(
            **{"Id": "different-request-uuid", "jobAdId": 270968802}
        )),
    )

    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)

    assert result == ("", "identity_mismatch")
    assert any(
        reason in result.identity_evidence
        for reason in ("reason:beisen_request_id_mismatch", "reason:beisen_job_ad_id_conflict")
    )


def test_beisen_missing_request_uuid_is_rejected_for_bound_row(monkeypatch) -> None:
    row = _beisen_row()
    payload = _beisen_payload()
    payload["Data"].pop("Id")
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=payload),
    )

    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)

    assert result[1] == "identity_mismatch"
    assert "reason:beisen_request_id_missing" in result.identity_evidence


def test_beisen_host_title_and_tenant_bindings_are_strict(monkeypatch) -> None:
    row = _beisen_row()
    row["tenant_id"] = "tenant-expected"

    def get(*_args, **_kwargs):
        return _Response(payload=_beisen_payload(**{"OrgId": "tenant-expected"}))

    monkeypatch.setattr(job_details.requests, "get", get)
    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)
    assert result[1] == "complete"

    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=_beisen_payload(
            **{"JobAdName": "另一岗位", "OrgId": "tenant-actual"}
        )),
    )
    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)
    assert result[1] == "identity_mismatch"

    row["source_tenant"] = "beisen:other.zhiye.com:/campus:"
    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)
    assert result[1] == "identity_mismatch"
    assert any(
        reason in result.identity_evidence
        for reason in ("reason:beisen_host_mismatch", "reason:beisen_tenant_host_conflict")
    )


def test_beisen_cross_host_redirect_is_rejected_before_payload_use(monkeypatch) -> None:
    row = _beisen_row()
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(
            url="https://other.zhiye.com/api/JobAd/GetJobAdInfo",
        ),
    )

    result = job_details.fetch_beisen_job_description_status(BEISEN_DETAIL_URL, identity=row)

    assert result == ("", "identity_mismatch")
    assert "response_host:other.zhiye.com" in result.identity_evidence
    assert "reason:beisen_redirect_host_mismatch" in result.identity_evidence


def test_beisen_mobile_detail_uses_lightbolt_info_and_binds_job_ad_id(monkeypatch) -> None:
    url = "https://sast.m.zhiye.com/#/jobdetail?id=511201819&jc=2&isReward=false"
    row = {
        "company_id": "rc-sast",
        "company": "上海空间电源研究所",
        "title": "化学电源研究师（AI人工智能方向）（2027校招）",
        "detail_url": url,
        "company_campus_url": "https://sast.m.zhiye.com/job.html?jc=2",
    }
    calls = []

    def get(request_url, **kwargs):
        calls.append((request_url, kwargs["params"]))
        return _Response(payload={
            "Code": 200,
            "Data": {
                "JobAdId": 511201819,
                "JobAdName": row["title"],
                "DutyStr": "负责人工智能方向化学电源研究与工程验证。" * 8,
                "RequireStr": "博士学历，材料、化学或人工智能相关专业。" * 8,
                "TenantId": 109911,
            },
        })

    monkeypatch.setattr(job_details.requests, "get", get)
    result = job_details.fetch_beisen_job_description_status(url, identity=row)

    assert result[1] == "complete"
    assert result.identity_status == "matched"
    assert "job_ad_id:JobAdId:511201819" in result.identity_evidence
    assert result.attempts == ("beisen_mobile_api:complete",)
    assert calls[0][0] == "https://sast.m.zhiye.com/LightBoltAPI/JobAd/Info"
    assert calls[0][1]["adid"] == "511201819"


def test_beisen_mobile_detail_rejects_wrong_job_ad_id(monkeypatch) -> None:
    url = "https://sast.m.zhiye.com/#/jobdetail?id=511201819&jc=2&isReward=false"
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload={
            "Code": 200,
            "Data": {
                "JobAdId": 511201800,
                "JobAdName": "另一岗位",
                "DutyStr": "岗位职责",
                "RequireStr": "任职要求",
            },
        }),
    )

    result = job_details.fetch_beisen_job_description_status(
        url,
        identity={
            "company_id": "rc-sast",
            "title": "化学电源研究师（AI人工智能方向）（2027校招）",
            "company_campus_url": "https://sast.m.zhiye.com/job.html?jc=2",
        },
    )

    assert result[1] == "identity_mismatch"
    assert "reason:beisen_request_job_ad_id_mismatch" in result.identity_evidence
