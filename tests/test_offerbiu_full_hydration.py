from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import threading
import time

import pytest

from scripts import hydrate_offerbiu_full_crawl as module


def _evidence(detail: str, url: str, *, method: str = "fixture_api", marker: str | None = None) -> dict[str, object]:
    evidence: dict[str, object] = {
        "status": "complete",
        "method": method,
        "source_url": url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(detail.encode("utf-8")).hexdigest(),
    }
    if marker is not None:
        evidence["marker"] = marker
    return evidence


def _pending_evidence(detail: str, url: str, *, marker: str | None = None) -> dict[str, object]:
    evidence = _evidence(detail, url, marker=marker)
    evidence["status"] = "incomplete"
    return evidence


def _official_hydration(
    detail: str,
    url: str,
    *,
    native_id: str,
    title: str,
    company: str | None = None,
    source: str = "moka_official",
) -> dict[str, object]:
    identity_evidence = [f"native_id:{native_id}", f"title:{title}"]
    if company is not None:
        identity_evidence.append(f"company:{company}")
    return {
        "detail": detail,
        "status": "complete",
        "source": source,
        "detail_url": url,
        "identity_status": "matched",
        "identity_evidence": identity_evidence,
        "capture_evidence": _evidence(detail, url, method="official_api"),
    }


def _job(
    key: str,
    *,
    native_id: str | None = None,
    source_job_id: str | None = None,
    detail_url: str | None = None,
    cohort: int = 2027,
    cohort_status: str = "confirmed",
    jd_raw: str = "",
    capture_evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "title": key,
        "cohort": cohort,
        "cohort_status": cohort_status,
        "jd_raw": jd_raw,
    }
    if source_job_id is not None:
        value["source_job_id"] = source_job_id
    elif native_id is not None:
        value["nativeID"] = native_id
    if detail_url is not None:
        value["detail_url"] = detail_url
    if capture_evidence is not None:
        value["capture_evidence"] = capture_evidence
    return value


def _write_checkpoint(
    directory: Path,
    name: str,
    company: str,
    jobs: list[dict[str, object]],
    *,
    crawl_url: str = "https://example.test/campus",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        json.dumps(
            {
                "company": company,
                "crawl_url": crawl_url,
                "crawl": {"raw_jobs": jobs},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_full_hydration_dedupes_skips_complete_and_preserves_failed_jd(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    existing_url = "https://example.test/jobs/existing"
    _write_checkpoint(
        input_dir,
        "one.json",
        "Example",
        [
            _job(
                "existing",
                native_id="native-1",
                detail_url=existing_url,
                jd_raw="already captured",
                capture_evidence=_evidence("already captured", existing_url),
            ),
            _job("duplicate", native_id="native-1", detail_url="https://example.test/jobs/other"),
            _job("needs hydration", native_id="native-2", detail_url="https://example.test/jobs/2"),
            _job("fails", native_id="native-3", detail_url="https://example.test/jobs/3", jd_raw="old text"),
            _job("unknown cohort", native_id="native-4", detail_url="https://example.test/jobs/4", cohort_status="unknown"),
        ],
    )

    calls: list[tuple[dict[str, object], float]] = []

    def fetch(job, *, timeout_seconds):
        calls.append((job, timeout_seconds))
        if job["nativeID"] == "native-3":
            raise RuntimeError("fixture failure")
        detail = "hydrated detail"
        url = job["detail_url"]
        return {
            "detail": detail,
            "status": "complete",
            "source": "fixture",
            "detail_url": url,
            "capture_evidence": _evidence(detail, url),
        }

    summary = module.run(input_dir, output_dir, workers=2, timeout=5, fetch=fetch)

    assert summary["stage_complete"] is True
    assert summary["complete"] == 1
    assert summary["hydrated"] == 2
    assert summary["failed"] == 1
    assert summary["cohort_unconfirmed_not_hydrated"] == 0
    assert summary["deduplicated_job_count"] == 4
    assert {job["nativeID"] for job, _ in calls} == {"native-2", "native-3", "native-4"}
    assert all(job["cohort"] == 2027 and job["cohort_status"] == "confirmed" for job, _ in calls)
    assert all(timeout == 5 for _, timeout in calls)

    rows = [json.loads(line) for line in (output_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]
    hydrated = next(row for row in rows if row.get("nativeID") == "native-2")
    assert hydrated["jd_raw"] == "hydrated detail"
    assert hydrated["job"]["jd_raw"] == "hydrated detail"
    assert hydrated["capture_evidence"] == hydrated["job"]["capture_evidence"]
    assert hydrated["capture_evidence"]["source_url"] == "https://example.test/jobs/2"
    assert hydrated["original_jd_raw"] == ""
    failed = next(row for row in rows if row.get("nativeID") == "native-3")
    assert failed["jd_raw"] == "old text"
    assert failed["job"]["jd_raw"] == "old text"
    assert failed["original_jd_raw"] == "old text"
    assert failed["hydration"]["status"] == "fetch_failed"
    assert failed["request_made"] is True
    assert all(row["model_calls"] == row["db_writes"] == 0 for row in rows)
    assert len(list((output_dir / "checkpoints").glob("*.json"))) == 4

    module.run(input_dir, output_dir, workers=2, timeout=5, resume=True, fetch=fetch)
    assert {job["nativeID"] for job, _ in calls} == {"native-2", "native-3", "native-4"}


def test_strong_cross_company_hydration_reuses_once_and_keeps_source_labels(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    url = "https://app.mokahr.com/social-recruitment/eaton/166618#/job/f1c09afe-0080-4136-91a6-379492daa63e"
    job_a = _job(
        "生产制造测试工程师，2027校招",
        source_job_id="f1c09afe-0080-4136-91a6-379492daa63e",
        detail_url=url,
        jd_raw="same raw JD",
    )
    job_a.update(
        {
            "company": "伊顿中国",
            "source": "伊顿中国",
            "source_list_url": "https://app.mokahr.com/social-recruitment/eaton/166618",
            "city": "",
            "job_type": "全职",
            "employment_type": "全职",
            "recruitment_track": "social",
            "cohort_source": "岗位标题",
            "cohort_evidence": "生产制造测试工程师，2027校招",
            "cohort_checked_at": "2026-09-08T21:08:59",
        }
    )
    job_b = dict(job_a)
    job_b.update(
        {
            "company": "伊顿(中国)",
            "source": "伊顿(中国)",
            "cohort_evidence": "同一官方岗位正文",
            "cohort_checked_at": "2026-09-08T21:04:53",
        }
    )
    _write_checkpoint(
        input_dir,
        "a.json",
        "伊顿中国",
        [job_a],
        crawl_url="https://a.example/campus",
    )
    _write_checkpoint(
        input_dir,
        "b.json",
        "伊顿(中国)",
        [job_b],
        crawl_url="https://b.example/campus",
    )

    calls: list[dict[str, object]] = []

    def fetch(job, *, timeout_seconds):
        del timeout_seconds
        calls.append(job)
        return _official_hydration(
            "verified JD",
            job["detail_url"],
            native_id=job["source_job_id"],
            title=job["title"],
        )

    summary = module.run(input_dir, output_dir, workers=2, timeout=3, fetch=fetch)
    rows = [json.loads(line) for line in (output_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]

    assert len(calls) == 1
    assert summary["hydrated"] == 2
    assert summary["attempted_job_count"] == 1
    assert {row["company"] for row in rows} == {"伊顿中国", "伊顿(中国)"}
    assert all(row["jd_raw"] == row["job"]["jd_raw"] == "verified JD" for row in rows)
    assert all(row["capture_evidence"] == row["job"]["capture_evidence"] for row in rows)
    leader = next(row for row in rows if row["company"] == "伊顿中国")
    reused = next(row for row in rows if row["company"] == "伊顿(中国)")
    assert leader["hydration_reused_from"] is None
    assert reused["hydration_reused_from"] == leader["job_key"]
    assert reused["source_labels"]["company"] == "伊顿(中国)"
    assert reused["hydration_reuse_source_labels"]["company"] == "伊顿中国"
    assert len(list((output_dir / "checkpoints").glob("*.json"))) == 2


def test_feishu_and_hotjob_detail_url_ids_supplement_missing_native_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    feishu_url = "https://xiaopeng.jobs.feishu.cn/398875/position/123/detail"
    hotjob_url = "https://acme.hotjob.cn/SU123/pb/posDetail.html?postId=456&postType=campus"
    feishu_a = _job("Feishu Engineer", detail_url=feishu_url, jd_raw="")
    feishu_b = _job("Feishu Engineer", detail_url=feishu_url, jd_raw="")
    hotjob_a = _job("Hotjob Engineer", detail_url=hotjob_url, jd_raw="")
    hotjob_a["jd_url"] = hotjob_a.pop("detail_url")
    hotjob_b = dict(hotjob_a)
    for job, company, checked_at in (
        (feishu_a, "Feishu Entity A", "2026-09-08T01:00:00"),
        (feishu_b, "Feishu Entity B", "2026-09-08T02:00:00"),
        (hotjob_a, "Hotjob Entity A", "2026-09-08T03:00:00"),
        (hotjob_b, "Hotjob Entity B", "2026-09-08T04:00:00"),
    ):
        job.update(
            {
                "company": company,
                "source": company,
                "source_list_url": f"https://{company.casefold().replace(' ', '-')}.example/source",
                "city": "上海",
                "job_type": "全职",
                "employment_type": "全职",
                "recruitment_track": "campus",
                "cohort_evidence": "observed cohort",
                "cohort_checked_at": checked_at,
            }
        )
    _write_checkpoint(input_dir, "feishu-a.json", "Feishu Entity A", [feishu_a])
    _write_checkpoint(input_dir, "feishu-b.json", "Feishu Entity B", [feishu_b])
    _write_checkpoint(input_dir, "hotjob-a.json", "Hotjob Entity A", [hotjob_a])
    _write_checkpoint(input_dir, "hotjob-b.json", "Hotjob Entity B", [hotjob_b])

    assert module._observed_url_native_id(feishu_a) == "123"
    assert module._observed_url_native_id(hotjob_a) == "456"
    assert module._observed_url_native_id({"detail_url": "https://xiaopeng.jobs.feishu.cn/398875/position/list"}) == ""

    calls: list[dict[str, object]] = []

    def fetch(job, *, timeout_seconds):
        del timeout_seconds
        calls.append(job)
        url = job.get("detail_url") or job["jd_url"]
        is_feishu = "feishu.cn" in url
        return _official_hydration(
            f"verified {job['title']}",
            url,
            native_id="123" if is_feishu else "456",
            title=job["title"],
            source="feishu_api" if is_feishu else "hotjob_api",
        )

    summary = module.run(input_dir, output_dir, workers=2, timeout=3, fetch=fetch)
    rows = [json.loads(line) for line in (output_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]

    assert len(calls) == 2
    assert summary["hydrated"] == 4
    assert summary["attempted_job_count"] == 2
    assert sum(row["hydration_reused_from"] is not None for row in rows) == 2
    assert all("source_job_id" not in row["job"] for row in rows)


def test_conflicting_explicit_and_url_native_ids_never_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    url = "https://xiaopeng.jobs.feishu.cn/398875/position/123/detail"
    first = _job("Conflict Engineer", source_job_id="999", detail_url=url, jd_raw="")
    second = dict(first)
    first["company"] = "Conflict A"
    second["company"] = "Conflict B"
    _write_checkpoint(input_dir, "a.json", "Conflict A", [first])
    _write_checkpoint(input_dir, "b.json", "Conflict B", [second])
    calls: list[dict[str, object]] = []

    def fetch(job, *, timeout_seconds):
        del timeout_seconds
        calls.append(job)
        return _official_hydration(
            "verified conflict JD",
            job["detail_url"],
            native_id="123",
            title=job["title"],
            source="feishu_api",
        )

    summary = module.run(input_dir, output_dir, workers=2, timeout=3, fetch=fetch)
    rows = [json.loads(line) for line in (output_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]

    assert len(calls) == 2
    assert summary["hydrated"] == 2
    assert all(row["hydration_reused_from"] is None for row in rows)


def test_any_identity_or_content_difference_blocks_cross_company_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    cases: list[tuple[str, dict[str, object], dict[str, object], bool]] = []

    def pair(label: str, first: dict[str, object], second: dict[str, object], *, company_evidence: bool = False):
        cases.append((label, first, second, company_evidence))

    pair(
        "exact",
        _job("same title", native_id="id-exact", detail_url="https://official.example/exact", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/exact")),
        _job("same title", native_id="id-exact", detail_url="https://official.example/exact", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/exact")),
    )
    pair(
        "title",
        _job("title A", native_id="id-title", detail_url="https://official.example/title", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/title")),
        _job("title B", native_id="id-title", detail_url="https://official.example/title", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/title")),
    )
    pair(
        "native",
        _job("same native", native_id="id-native-a", detail_url="https://official.example/native", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/native")),
        _job("same native", native_id="id-native-b", detail_url="https://official.example/native", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/native")),
    )
    pair(
        "cohort-status",
        _job("same status", native_id="id-status", detail_url="https://official.example/status", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/status")),
        _job("same status", native_id="id-status", detail_url="https://official.example/status", cohort_status="Confirmed", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/status")),
    )
    pair(
        "jd",
        _job("same JD", native_id="id-jd", detail_url="https://official.example/jd", jd_raw="JD A", capture_evidence=_pending_evidence("JD A", "https://official.example/jd")),
        _job("same JD", native_id="id-jd", detail_url="https://official.example/jd", jd_raw="JD B", capture_evidence=_pending_evidence("JD B", "https://official.example/jd")),
    )
    pair(
        "capture",
        _job("same capture", native_id="id-capture", detail_url="https://official.example/capture", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/capture", marker="A")),
        _job("same capture", native_id="id-capture", detail_url="https://official.example/capture", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/capture", marker="B")),
    )
    pair(
        "url",
        _job("same URL?", native_id="id-url", detail_url="https://official.example/url-a", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/url-a")),
        _job("same URL?", native_id="id-url", detail_url="https://official.example/url-b", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/url-b")),
    )
    pair(
        "company-evidence",
        _job("company checked", native_id="id-company", detail_url="https://official.example/company", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/company")),
        _job("company checked", native_id="id-company", detail_url="https://official.example/company", jd_raw="same JD", capture_evidence=_pending_evidence("same JD", "https://official.example/company")),
        company_evidence=True,
    )

    for index, (label, first, second, _) in enumerate(cases):
        _write_checkpoint(input_dir, f"{index:02d}-{label}-a.json", f"Company {label} A", [first])
        _write_checkpoint(input_dir, f"{index:02d}-{label}-b.json", f"Company {label} B", [second])

    calls: list[dict[str, object]] = []

    def fetch(job, *, timeout_seconds):
        del timeout_seconds
        calls.append(job)
        company = job["company"]
        company_marker = "company" in str(company).casefold() and "company-evidence" in str(company)
        return _official_hydration(
            f"verified {job['title']}",
            job["detail_url"],
            native_id=job["nativeID"],
            title=job["title"],
            company=str(company) if company_marker else None,
        )

    module.run(input_dir, output_dir, workers=3, timeout=3, fetch=fetch)
    rows = [json.loads(line) for line in (output_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]

    # OfferBiu cohort values are normalized before reuse comparison.
    assert len(calls) == len(cases) * 2 - 2
    assert sum(row["hydration_reused_from"] is not None for row in rows) == 2
    assert all(
        row["hydration_reused_from"] is None
        for row in rows
        if row["company"].startswith("Company ")
        and not any(label in row["company"] for label in ("exact", "cohort-status"))
    )


def test_failed_reuse_representative_falls_back_without_overwriting_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    url = "https://official.example/jobs/failure"
    job_kwargs = {
        "native_id": "id-failure",
        "detail_url": url,
        "jd_raw": "original JD",
        "capture_evidence": _pending_evidence("original JD", url),
    }
    _write_checkpoint(input_dir, "a.json", "Failing Entity", [_job("same failure", **job_kwargs)])
    _write_checkpoint(input_dir, "b.json", "Second Entity", [_job("same failure", **job_kwargs)])
    calls: list[str] = []

    def fetch(job, *, timeout_seconds):
        del timeout_seconds
        calls.append(job["company"])
        if job["company"] == "Failing Entity":
            raise RuntimeError("representative failed")
        return _official_hydration(
            "second verified JD",
            job["detail_url"],
            native_id=job["nativeID"],
            title=job["title"],
        )

    summary = module.run(input_dir, output_dir, workers=2, timeout=3, fetch=fetch)
    rows = [json.loads(line) for line in (output_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]
    failed = next(row for row in rows if row["company"] == "Failing Entity")
    succeeded = next(row for row in rows if row["company"] == "Second Entity")

    assert calls == ["Failing Entity", "Second Entity"]
    assert summary["failed"] == 1
    assert summary["hydrated"] == 1
    assert failed["hydration_reused_from"] is None
    assert failed["jd_raw"] == failed["job"]["jd_raw"] == "original JD"
    assert failed["capture_evidence"] == failed["job"]["capture_evidence"]
    assert failed["candidate_jd_raw"] == ""
    assert succeeded["hydration_reused_from"] is None
    assert succeeded["jd_raw"] == succeeded["job"]["jd_raw"] == "second verified JD"


def test_url_and_exact_hash_identity_are_used_without_samples_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    same_url = "https://example.test/jobs/1?utm_source=one"
    _write_checkpoint(
        input_dir,
        "one.json",
        "Example",
        [
            _job("url one", detail_url=same_url),
            _job("url duplicate", detail_url="https://example.test/jobs/1?utm_source=two"),
            _job("hash only"),
            _job("hash only"),
        ],
    )

    calls = []

    def fetch(job, *, timeout_seconds):
        calls.append(job)
        detail = f"detail for {job['title']}"
        return {
            "detail": detail,
            "status": "complete",
            "capture_evidence": _evidence(detail, job.get("detail_url") or "https://example.test/jobs/hash"),
        }

    summary = module.run(input_dir, output_dir, workers=1, timeout=3, fetch=fetch)

    assert summary["unique_job_count"] == 2
    assert len(calls) == 2
    assert summary["hydrated"] == 2
    assert all(row["identity"]["type"] in {"company_detail_url", "company_job_hash"} for row in [json.loads(line) for line in (output_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()])


def test_bad_capture_hash_is_failed_and_old_jd_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    url = "https://example.test/jobs/hash-check"
    _write_checkpoint(
        input_dir,
        "one.json",
        "Example",
        [_job("hash check", native_id="native-hash", detail_url=url, jd_raw="original JD")],
    )

    def fetch(_job, *, timeout_seconds):
        del timeout_seconds
        return {
            "detail": "new JD",
            "status": "complete",
            "capture_evidence": _evidence("different JD", url),
        }

    summary = module.run(input_dir, output_dir, workers=1, timeout=2, fetch=fetch)
    row = json.loads((output_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert summary["failed"] == 1
    assert summary["hydrated"] == 0
    assert row["hydration_outcome"] == "failed"
    assert row["jd_raw"] == "original JD"
    assert row["job"]["jd_raw"] == "original JD"
    assert row["capture_evidence"] == row["job"].get("capture_evidence")
    assert row["capture_evidence"] is None
    assert row["candidate_jd_raw"] == "new JD"
    assert row["failure_reason"] == "capture:capture_content_changed"


def test_native_id_scope_includes_normalized_detail_host(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    _write_checkpoint(
        input_dir,
        "one.json",
        "Example",
        [
            _job("ATS one", native_id="42", detail_url="https://ats-one.example/jobs/42"),
            _job("ATS two", native_id="42", detail_url="https://ats-two.example/jobs/42"),
        ],
    )
    calls = []

    def fetch(job, *, timeout_seconds):
        calls.append(job)
        detail = f"detail for {job['title']}"
        return {
            "detail": detail,
            "status": "complete",
            "capture_evidence": _evidence(detail, job["detail_url"]),
        }

    summary = module.run(input_dir, output_dir, workers=1, timeout=2, fetch=fetch)
    rows = [json.loads(line) for line in (output_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary["unique_job_count"] == 2
    assert len(calls) == 2
    assert {row["identity"]["value"].split(":")[1] for row in rows} == {"ats-one.example", "ats-two.example"}


def test_hydration_concurrency_is_bounded_and_output_guard_remains_active(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    _write_checkpoint(
        input_dir,
        "one.json",
        "Example",
        [_job(str(index), native_id=f"native-{index}", detail_url=f"https://example.test/{index}") for index in range(6)],
    )
    lock = threading.Lock()
    active = 0
    peak = 0

    def fetch(job, *, timeout_seconds):
        nonlocal active, peak
        assert timeout_seconds == 4
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        detail = f"detail {job['nativeID']}"
        with lock:
            active -= 1
        return {"detail": detail, "status": "complete", "capture_evidence": _evidence(detail, job["detail_url"])}

    summary = module.run(input_dir, output_dir, workers=3, timeout=4, fetch=fetch)
    assert summary["stage_complete"] is True
    assert peak <= 3

    with pytest.raises(ValueError, match="Output"):
        module.run(input_dir, tmp_path / "outside", fetch=fetch)


def test_hydration_worker_limit_and_summary_counts_are_incremental(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    input_dir = tmp_path / ".data" / "evals" / "crawl" / "checkpoints"
    output_dir = tmp_path / ".data" / "evals" / "hydration"
    _write_checkpoint(
        input_dir,
        "one.json",
        "Example",
        [_job(str(index), native_id=f"native-{index}", detail_url=f"https://example.test/{index}") for index in range(3)],
    )

    summary_calls = []
    original_summary = module._summary

    def summary_spy(*args, **kwargs):
        summary_calls.append(kwargs)
        return original_summary(*args, **kwargs)

    monkeypatch.setattr(module, "_summary", summary_spy)

    def fetch(job, *, timeout_seconds):
        del timeout_seconds
        detail = f"detail {job['nativeID']}"
        return {"detail": detail, "status": "complete", "capture_evidence": _evidence(detail, job["detail_url"])}

    summary = module.run(input_dir, output_dir, workers=6, timeout=4, fetch=fetch)

    assert module.MAX_WORKERS == 12
    assert summary["stage_complete"] is True
    assert summary["hydrated"] == 3
    assert summary_calls
    assert all("status_counts" in call and "attempted_job_count" in call for call in summary_calls)


def test_jobs_jsonl_writer_does_not_build_all_lines_in_memory(tmp_path, monkeypatch):
    path = tmp_path / "jobs.jsonl"
    records = [{"job_key": "one"}, {"job_key": "two"}]
    results = {
        "one": {"job_key": "one", "value": "a"},
        "two": {"job_key": "two", "value": "b"},
    }

    monkeypatch.setattr(module, "_write_text", lambda *_args, **_kwargs: pytest.fail("buffered writer used"))
    module._write_jobs(path, records, results)

    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == list(results.values())


def test_atomic_json_retries_temporary_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "summary.json"
    target.write_text('{"old": true}', encoding="utf-8")
    original_replace = Path.replace
    calls = []
    delays = []

    def replace(path, destination):
        calls.append(path)
        if len(calls) < 3:
            assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
            raise PermissionError("file temporarily locked")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(module, "sleep", delays.append)
    module._write_json(target, {"new": True})
    assert len(calls) == 3
    assert delays == [0.1, 0.2]
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


def test_progress_lock_is_nonfatal_but_checkpoints_are_strict(tmp_path, monkeypatch, capsys):
    def locked(*args):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr(module, "sleep", lambda _: None)
    module._write_progress(tmp_path / "summary.json", {"pending": 1})
    assert "Progress update deferred" in capsys.readouterr().err
    with pytest.raises(PermissionError):
        module._write_json(tmp_path / "checkpoint.json", {"job_key": "one"})


def test_atomic_json_does_not_hide_other_io_errors(tmp_path, monkeypatch):
    def no_space(*args):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", no_space)
    monkeypatch.setattr(module, "sleep", lambda _: pytest.fail("unexpected retry"))
    with pytest.raises(OSError, match="disk full"):
        module._write_progress(tmp_path / "summary.json", {})
