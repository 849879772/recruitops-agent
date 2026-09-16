import json
import hashlib
import unicodedata
from pathlib import Path

from packages.recruitment_core import job_details
from scripts import repair_lenovo_catalog_batch as batch


def _row(**overrides):
    row = {
        "id": "",
        "company_id": "config-277",
        "company_name": "联想",
        "title": "产品经理-AI方向",
        "city": "1,6",
        "detail_url": "https://talent.lenovo.com.cn/position/detail?id=2362",
        "company_campus_url": "https://talent.lenovo.com.cn/position",
        "jd_raw": "",
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": "formal",
        "source_platform": "lenovo",
        "source_tenant": None,
        "native_job_id": "",
        "source_job_id": None,
        "company_crawler_key": "lenovo",
        "model": None,
        "analysis_status": "jd_incomplete",
    }
    row.update(overrides)
    encoded = "\x00".join(
        " ".join(
            unicodedata.normalize("NFKC", str(row.get(field) or "")).split()
        )
        for field in ("company_id", "detail_url", "title", "city")
    )
    synthetic_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if "id" not in overrides:
        row["id"] = synthetic_id
    if "native_job_id" not in overrides:
        row["native_job_id"] = row["id"]
    return row


class _Response:
    def __init__(self, payload, *, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise job_details.requests.HTTPError(f"HTTP {self.status_code}")


def _payload(*, job_id=2362, title="产品经理-AI方向"):
    return {
        "code": 0,
        "result": {
            "rows": [
                {
                    "id": job_id,
                    "jobId": job_id,
                    "jobName": title,
                    "jobDuties": "<p>负责 AI 产品规划、需求分析与跨团队推进。</p>",
                    "jobRequirement": "<p>本科及以上，熟悉人工智能产品和数据分析。</p>",
                    "workPlace": "1,6",
                }
            ]
        },
    }


def test_lenovo_route_requires_exact_official_detail_id() -> None:
    assert job_details._lenovo_route_id(_row()["detail_url"]) == "2362"
    assert job_details._lenovo_route_id("https://talent.lenovo.com.cn/position") == ""
    assert job_details._lenovo_route_id("https://other.example/position/detail?id=2362") == ""
    assert job_details._lenovo_route_id("https://talent.lenovo.com.cn/position/detail?id=x") == ""


def test_lenovo_public_api_binds_id_title_company_and_combines_fields(monkeypatch) -> None:
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return _Response(_payload())

    monkeypatch.setattr(job_details.requests, "get", fake_get)

    result = job_details.fetch_lenovo_job_description_status(
        _row()["detail_url"], identity={**_row(), "company": "联想"}
    )

    assert result[1] == "complete"
    assert "岗位职责" in result[0]
    assert "任职要求" in result[0]
    assert result.identity_status == "matched"
    assert "native_id:2362" in result.identity_evidence
    assert "title:产品经理-AI方向" in result.identity_evidence
    assert "company:联想" in result.identity_evidence
    assert seen["url"] == "https://talent.lenovo.com.cn/gateway/jobBase/list"
    assert seen["params"] == {"jobId": "2362"}
    assert "token" not in seen["headers"]
    assert "Cookie" not in seen["headers"]


def test_lenovo_rejects_title_or_id_mismatch_without_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(_payload(title="错误岗位")),
    )

    result = job_details.fetch_lenovo_job_description_status(
        _row()["detail_url"], identity={**_row(), "company": "联想"}
    )

    assert result[1] == "identity_mismatch"
    assert result[0] == ""
    assert "title:错误岗位" in result.identity_evidence


def test_lenovo_accepts_matching_numeric_native_id(monkeypatch) -> None:
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(_payload()),
    )

    result = job_details.fetch_lenovo_job_description_status(
        _row(native_job_id="2362")["detail_url"],
        identity={**_row(native_job_id="2362"), "company": "联想"},
    )

    assert result[1] == "complete"
    assert "native_id:2362" in result.identity_evidence


def test_lenovo_rejects_conflicting_numeric_native_id_before_http(monkeypatch) -> None:
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("conflicting stored id must be rejected before HTTP")
        ),
    )
    row = _row(native_job_id="9999")

    result = job_details.fetch_lenovo_job_description_status(
        row["detail_url"], identity={**row, "company": "联想"}
    )

    assert result[1] == "identity_mismatch"
    assert "reason:lenovo_native_job_id_mismatch" in result.identity_evidence


def test_lenovo_rejects_conflicting_native_and_source_ids_before_http(monkeypatch) -> None:
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("conflicting dual ids must be rejected before HTTP")
        ),
    )
    row = _row(native_job_id="2362", source_job_id="9999")

    result = job_details.fetch_lenovo_job_description_status(
        row["detail_url"], identity={**row, "company": "联想"}
    )

    assert result[1] == "identity_mismatch"
    assert "reason:lenovo_source_job_id_mismatch" in result.identity_evidence


def test_lenovo_rejects_non_synthetic_hash_conflict_before_http(monkeypatch) -> None:
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-synthetic hash must not be treated as a row id")
        ),
    )
    row = _row(native_job_id="a" * 64)

    result = job_details.fetch_lenovo_job_description_status(
        row["detail_url"], identity={**row, "company": "联想"}
    )

    assert result[1] == "identity_mismatch"
    assert "reason:lenovo_native_job_id_mismatch" in result.identity_evidence


def test_lenovo_rejects_conflicting_api_id_fields(monkeypatch) -> None:
    payload = _payload()
    payload["result"]["rows"][0]["jobId"] = 9999
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload),
    )

    result = job_details.fetch_lenovo_job_description_status(
        _row()["detail_url"], identity={**_row(), "company": "联想"}
    )

    assert result[1] == "identity_mismatch"
    assert "native_id:2362" in result.identity_evidence
    assert "native_id:9999" in result.identity_evidence


def test_lenovo_rejects_cross_host_or_unbound_company_project_before_http(monkeypatch) -> None:
    def unexpected_get(*_args, **_kwargs):
        raise AssertionError("unbound Lenovo row must not call the API")

    monkeypatch.setattr(job_details.requests, "get", unexpected_get)
    result = job_details.fetch_lenovo_job_description_status(
        _row(company_campus_url="https://other.example/position")["detail_url"],
        identity={**_row(company_campus_url="https://other.example/position"), "company": "联想"},
    )

    assert result[1] == "identity_mismatch"
    assert "reason:lenovo_company_host_mismatch" in result.identity_evidence


def test_lenovo_rejects_positionevil_project_path_before_http(monkeypatch) -> None:
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid project path must be rejected before HTTP")
        ),
    )
    row = _row(company_campus_url="https://talent.lenovo.com.cn/positionevil")

    result = job_details.fetch_lenovo_job_description_status(
        row["detail_url"], identity={**row, "company": "联想"}
    )

    assert result[1] == "identity_mismatch"
    assert "reason:lenovo_project_binding_missing" in result.identity_evidence


def test_lenovo_preserves_access_denied_and_does_not_guess_login(monkeypatch) -> None:
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: _Response({}, status_code=403),
    )

    result = job_details.fetch_lenovo_job_description_status(
        _row()["detail_url"], identity={**_row(), "company": "联想"}
    )

    assert result[1] == "access_denied"
    assert result.error_type == "HTTP403"
    assert result[0] == ""


def test_full_hydration_uses_lenovo_branch_without_render_fallback(monkeypatch) -> None:
    candidate = "岗位职责\n" + "负责产品规划、需求分析和研发协作。" * 4 + "\n任职要求\n本科及以上，熟悉 AI 产品。"
    called = {}

    def fake_lenovo(url, *, identity):
        called["url"] = url
        called["identity"] = identity
        return job_details._DetailStatus(
            candidate,
            "complete",
            detail_url=url,
            identity_status="matched",
            identity_evidence=("native_id:2362", "title:产品经理-AI方向"),
        )

    monkeypatch.setattr(job_details, "fetch_lenovo_job_description_status", fake_lenovo)
    monkeypatch.setattr(
        job_details,
        "render_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no render fallback")),
    )

    result = job_details.fetch_full_job_description_result(_row())

    assert result.status == "complete"
    assert result.source == "lenovo_official_api"
    assert called["url"] == _row()["detail_url"]


def test_runner_record_is_schema1_importer_compatible(monkeypatch) -> None:
    candidate = "岗位职责\n" + "负责产品规划、需求分析和研发协作。" * 8 + "\n任职要求\n本科及以上，熟悉 AI 产品。"
    monkeypatch.setattr(
        batch,
        "fetch_job_detail_result_isolated",
        lambda *_args, **_kwargs: {
            "detail": candidate,
            "status": "complete",
            "source": "lenovo_official_api",
            "detail_url": _row()["detail_url"],
            "attempts": ["lenovo_detail_api:complete"],
            "error_type": "",
            "identity_status": "matched",
            "identity_evidence": ["native_id:2362", "title:产品经理-AI方向"],
        },
    )

    item = batch._record_for_row(
        _row(),
        timeout_seconds=2,
        retries=0,
        deadline=10**9,
        limiters=batch._HostLimiters(1),
    )

    assert item["selection"]["selected"] is True
    assert item["validation"]["passed"] is True
    assert item["candidate_jd"] == candidate
    assert item["original_sha256"]
    assert item["candidate_sha256"]
    assert {"job_id", "original_sha256", "candidate_jd", "candidate_sha256", "validation"} <= item.keys()


def test_runner_preserves_diagnostic_and_emits_read_only_report(monkeypatch, tmp_path: Path) -> None:
    diagnostic = {"schema": 1, "status": "confirmed"}
    output = tmp_path / "lenovo-wave01"
    output.mkdir()
    (output / "diagnostic.json").write_text(
        json.dumps(diagnostic), encoding="utf-8"
    )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            return None

    monkeypatch.setattr(batch, "_readonly_engine", lambda _url: Engine())
    monkeypatch.setattr(batch, "_set_sqlite_read_only", lambda _connection: None)
    monkeypatch.setattr(batch, "_query_rows", lambda _connection, *, limit: [_row()])
    candidate = "岗位职责\n" + "负责产品规划和研发协作。" * 8 + "\n任职要求\n本科及以上。"
    monkeypatch.setattr(
        batch,
        "fetch_job_detail_result_isolated",
        lambda *_args, **_kwargs: {
            "detail": candidate,
            "status": "complete",
            "source": "lenovo_official_api",
            "detail_url": _row()["detail_url"],
            "attempts": [],
            "error_type": "",
            "identity_status": "matched",
            "identity_evidence": ["native_id:2362", "title:产品经理-AI方向"],
        },
    )
    args = batch._parser().parse_args(
        [
            "--limit", "1",
            "--output", str(output),
            "--max-workers", "1",
            "--per-host-concurrency", "1",
            "--retries", "0",
            "--total-budget-seconds", "30",
        ]
    )

    report = batch.run(args)
    saved = json.loads((output / "report.json").read_text(encoding="utf-8"))

    assert report["status"] == "complete"
    assert saved["read_only"] is True
    assert saved["model_calls"] == 0
    assert saved["database_writes"] == 0
    assert saved["summary"]["passed"] == 1
    assert json.loads((output / "diagnostic.json").read_text(encoding="utf-8")) == diagnostic
