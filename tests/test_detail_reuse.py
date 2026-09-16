from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from threading import Event, Lock, get_ident
from time import sleep

import pytest

from packages.recruitment_core.detail_reuse import (
    DEFAULT_MAX_ENTRIES,
    ReusingDetailHydrator,
    build_detail_reuse_key,
)


BYTE_URL = "https://jobs.bytedance.com/campus/position/7667551275686594821/detail"
FEISHU_URL = "https://tenant-a.jobs.feishu.cn/campus/position/101/detail"
DETAIL = "岗位职责\n负责构建可验证的招聘平台服务。"


def _capture(url: str, detail: str = DETAIL, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "complete",
        "method": "official_api",
        "source_url": url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(detail.strip().encode("utf-8")).hexdigest(),
    }
    value.update(overrides)
    return value


def _result(
    url: str = BYTE_URL,
    *,
    post_id: str = "7667551275686594821",
    title: str = "Platform Engineer",
    detail: str = DETAIL,
    **overrides: object,
) -> dict[str, object]:
    result: dict[str, object] = {
        "detail": detail,
        "status": "complete",
        "source": "bytedance_api",
        "detail_url": url,
        "identity_status": "matched",
        "identity_evidence": (f"native_id:{post_id}", f"title:{title}"),
        "capture_evidence": _capture(url, detail),
        "_detail_reuse": {"reused": True, "wrong": "must be overwritten"},
    }
    result.update(overrides)
    return result


def _job(
    url: str = BYTE_URL,
    *,
    title: str = "Platform Engineer",
    source_company: str = "source-a",
    **overrides: object,
) -> dict[str, object]:
    job: dict[str, object] = {
        "title": title,
        "detail_url": url,
        "source_company": source_company,
        "id": "internal-synthetic-row-id",
    }
    job.update(overrides)
    return job


def test_same_bytedance_post_from_nine_sources_fetches_once_and_keeps_sources() -> None:
    calls: list[str] = []
    lock = Lock()

    def fetch(job, *, timeout_seconds):
        with lock:
            calls.append(job["source_company"])
        return _result()

    hydrator = ReusingDetailHydrator(fetch)
    jobs = [_job(source_company=f"source-{index}") for index in range(9)]
    original_jobs = deepcopy(jobs)
    results = [hydrator(job, timeout_seconds=1) for job in jobs]

    assert len(calls) == 1
    assert jobs == original_jobs
    assert all(result["detail"] == DETAIL for result in results)
    assert results[0]["_detail_reuse"]["reused"] is False
    assert results[0]["_detail_reuse"]["request_made"] is True
    assert all(result["_detail_reuse"]["reused"] is True for result in results[1:])
    assert all(result["_detail_reuse"]["request_made"] is False for result in results[1:])
    assert all(result["_detail_reuse"]["mode"] == "cache" for result in results[1:])
    assert hydrator.snapshot()["cache_entries"] == 1


@pytest.mark.parametrize(
    "result_update",
    [
        {"identity_evidence": ("native_id:wrong", "title:Platform Engineer")},
        {"identity_evidence": ()},
        {"status": "fetch_failed", "detail": ""},
        {"capture_evidence": {}},
    ],
)
def test_unverified_or_failed_results_are_not_reused(result_update: dict[str, object]) -> None:
    calls = 0

    def fetch(_job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        return _result(**result_update)

    hydrator = ReusingDetailHydrator(fetch)
    first = hydrator(_job(source_company="source-a"), timeout_seconds=1)
    second = hydrator(_job(source_company="source-b"), timeout_seconds=1)

    assert calls == 2
    assert first["_detail_reuse"]["reused"] is False
    assert second["_detail_reuse"]["reused"] is False
    assert first["_detail_reuse"]["request_made"] is True
    assert second["_detail_reuse"]["request_made"] is True
    assert hydrator.snapshot()["cache_entries"] == 0


def test_different_tenants_and_titles_do_not_collide() -> None:
    calls: list[tuple[str, str]] = []

    def fetch(job, *, timeout_seconds):
        calls.append((job["detail_url"], job["title"]))
        url = job["detail_url"]
        post_id = "101" if "101" in url else "102"
        return _result(url, post_id=post_id, title=job["title"])

    hydrator = ReusingDetailHydrator(fetch)
    tenant_a = _job(FEISHU_URL, title="Platform Engineer", tenant_id="tenant-a")
    tenant_b = _job(
        FEISHU_URL.replace("tenant-a", "tenant-b"),
        title="Platform Engineer",
        tenant_id="tenant-b",
    )
    different_title = _job(FEISHU_URL, title="Data Engineer", tenant_id="tenant-a")

    hydrator(tenant_a, timeout_seconds=1)
    hydrator(tenant_b, timeout_seconds=1)
    hydrator(different_title, timeout_seconds=1)

    assert len(calls) == 3
    assert build_detail_reuse_key(tenant_a) != build_detail_reuse_key(tenant_b)
    assert build_detail_reuse_key(tenant_a) != build_detail_reuse_key(different_title)


def test_explicit_post_id_conflict_disables_reuse_but_generic_ids_do_not() -> None:
    url = "https://tenant-a.jobs.feishu.cn/398875/position/123/detail"
    calls = 0

    def fetch(job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        return _result(url, post_id="123", title=job["title"])

    valid = _job(
        url,
        source_company="source-a",
        source_job_id="123",
        id="internal-row-a",
        internal_job_id="999",
    )
    conflicting = _job(
        url,
        source_company="source-b",
        source_job_id="999",
        id="internal-row-b",
        internal_job_id="123",
    )

    hydrator = ReusingDetailHydrator(fetch)
    first = hydrator(valid, timeout_seconds=1)
    second = hydrator(conflicting, timeout_seconds=1)

    assert build_detail_reuse_key(valid) is not None
    assert build_detail_reuse_key(conflicting) is None
    assert calls == 2
    assert first["_detail_reuse"]["request_made"] is True
    assert second["_detail_reuse"]["request_made"] is True


@pytest.mark.parametrize(
    "url",
    [
        "https://user@jobs.bytedance.com/campus/position/7667551275686594821/detail",
        "https://www.jobs.bytedance.com/campus/position/7667551275686594821/detail",
    ],
)
def test_original_url_security_signals_disable_reuse_before_normalization(url: str) -> None:
    calls = 0

    def fetch(job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        return _result(
            url=job["detail_url"],
            capture_evidence=_capture(job["detail_url"]),
        )

    job = _job(url)
    hydrator = ReusingDetailHydrator(fetch)

    first = hydrator(job, timeout_seconds=1)
    second = hydrator({**job, "source_company": "source-b"}, timeout_seconds=1)

    assert build_detail_reuse_key(job) is None
    assert calls == 2
    assert first["_detail_reuse"]["request_made"] is True
    assert second["_detail_reuse"]["request_made"] is True


@pytest.mark.parametrize(
    ("result_field", "bad_url"),
    [
        (
            "detail_url",
            "https://user@jobs.bytedance.com/campus/position/7667551275686594821/detail",
        ),
        (
            "capture_source_url",
            "https://www.jobs.bytedance.com/campus/position/7667551275686594821/detail",
        ),
    ],
)
def test_response_urls_are_raw_validated_before_reuse(
    result_field: str,
    bad_url: str,
) -> None:
    calls = 0

    def fetch(_job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        result = _result()
        if result_field == "detail_url":
            result["detail_url"] = bad_url
        else:
            result["capture_evidence"] = _capture(bad_url)
        return result

    hydrator = ReusingDetailHydrator(fetch)
    first = hydrator(_job(source_company="source-a"), timeout_seconds=1)
    second = hydrator(_job(source_company="source-b"), timeout_seconds=1)

    assert calls == 2
    assert first["_detail_reuse"]["request_made"] is True
    assert second["_detail_reuse"]["request_made"] is True
    assert first["_detail_reuse"]["cacheable"] is False
    assert second["_detail_reuse"]["cacheable"] is False


def test_response_title_mismatch_and_capture_proof_mismatch_are_not_cached() -> None:
    calls = 0

    def fetch(job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        return _result(
            title=job["title"],
            identity_evidence=("native_id:7667551275686594821", "title:Other Role"),
        )

    hydrator = ReusingDetailHydrator(fetch)
    result = hydrator(_job(), timeout_seconds=1)
    again = hydrator(_job(source_company="source-b"), timeout_seconds=1)

    assert calls == 2
    assert result["_detail_reuse"]["reason"] == "title_evidence_mismatch"
    assert again["_detail_reuse"]["reason"] == "title_evidence_mismatch"


def test_cache_and_metadata_are_deep_copied_and_wrapper_metadata_wins() -> None:
    calls = 0
    nested = {"items": ["original"]}

    def fetch(_job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        result = _result()
        result["nested"] = nested
        return result

    hydrator = ReusingDetailHydrator(fetch)
    first = hydrator(_job(), timeout_seconds=1)
    first["nested"]["items"].append("caller-mutation")
    first["_detail_reuse"]["reused"] = "spoofed"
    second = hydrator(_job(source_company="source-b"), timeout_seconds=1)

    assert calls == 1
    assert second["nested"] == {"items": ["original"]}
    assert second["_detail_reuse"]["reused"] is True
    assert second["_detail_reuse"]["request_made"] is False
    assert second["_detail_reuse"]["mode"] == "cache"
    assert nested == {"items": ["original"]}


def test_concurrent_same_key_has_one_leader_in_caller_thread_and_bounded_waiters() -> None:
    calls: list[int] = []
    lock = Lock()

    def fetch(_job, *, timeout_seconds):
        with lock:
            calls.append(get_ident())
        sleep(0.08)
        return _result()

    hydrator = ReusingDetailHydrator(fetch)
    jobs = [_job(source_company=f"source-{index}") for index in range(9)]
    with ThreadPoolExecutor(max_workers=9) as pool:
        results = list(pool.map(lambda item: hydrator(item, timeout_seconds=1), jobs))

    assert len(calls) == 1
    assert results[0]["detail"] == DETAIL
    assert sum(result["_detail_reuse"]["mode"] == "singleflight" for result in results) == 8
    assert hydrator.snapshot()["singleflight_waits"] == 8


def test_unverified_complete_singleflight_waiter_never_receives_success_body() -> None:
    calls = 0
    started = Event()
    release = Event()

    def fetch(_job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1)
        result = _result()
        result["capture_evidence"] = {
            **result["capture_evidence"],
            "content_sha256": "0" * 64,
        }
        return result

    hydrator = ReusingDetailHydrator(fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(hydrator, _job(source_company="source-a"), timeout_seconds=1)
        assert started.wait(1)
        waiter = pool.submit(hydrator, _job(source_company="source-b"), timeout_seconds=1)
        release.set()
        leader_result, waiter_result = leader.result(1), waiter.result(1)

    assert calls == 1
    assert leader_result["status"] == "complete"
    assert leader_result["detail"] == DETAIL
    assert waiter_result["status"] == "official_unverified"
    assert waiter_result["detail"] == ""
    assert waiter_result["_detail_reuse"]["request_made"] is False
    assert waiter_result["_detail_reuse"]["reused"] is False

    retry = hydrator(_job(source_company="source-c"), timeout_seconds=1)
    assert calls == 2
    assert retry["status"] == "complete"
    assert retry["_detail_reuse"]["request_made"] is True


def test_exception_leader_still_raises_but_waiter_gets_structured_no_request_failure() -> None:
    calls = 0
    started = Event()
    release = Event()

    def fetch(_job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1)
        raise RuntimeError("fixture fetch failure")

    hydrator = ReusingDetailHydrator(fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(hydrator, _job(source_company="source-a"), timeout_seconds=1)
        assert started.wait(1)
        waiter = pool.submit(hydrator, _job(source_company="source-b"), timeout_seconds=1)
        release.set()
        with pytest.raises(RuntimeError, match="fixture fetch failure"):
            leader.result(1)
        waiter_result = waiter.result(1)

    assert calls == 1
    assert waiter_result["status"] == "fetch_failed"
    assert waiter_result["_detail_reuse"]["request_made"] is False
    assert waiter_result["_detail_reuse"]["reused"] is False
    with pytest.raises(RuntimeError, match="fixture fetch failure"):
        hydrator(_job(source_company="source-c"), timeout_seconds=1)
    assert calls == 2


def test_failed_singleflight_waiters_receive_same_failure_without_retry() -> None:
    calls = 0
    started = Event()
    release = Event()

    def fetch(_job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1)
        return {"status": "fetch_failed", "detail": "", "error_type": "FixtureFailure"}

    hydrator = ReusingDetailHydrator(fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(hydrator, _job(source_company="source-a"), timeout_seconds=1)
        assert started.wait(1)
        waiter = pool.submit(hydrator, _job(source_company="source-b"), timeout_seconds=1)
        release.set()
        first, second = leader.result(1), waiter.result(1)

    retry = hydrator(_job(source_company="source-c"), timeout_seconds=1)
    assert calls == 2
    assert first["status"] == second["status"] == retry["status"] == "fetch_failed"
    assert second["_detail_reuse"]["reused"] is False
    assert second["_detail_reuse"]["request_made"] is False


def test_waiter_timeout_does_not_add_another_fetch_or_extend_leader_deadline() -> None:
    calls = 0
    started = Event()
    release = Event()

    def fetch(_job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1)
        return _result()

    hydrator = ReusingDetailHydrator(fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(hydrator, _job(), timeout_seconds=0.4)
        assert started.wait(1)
        waiter = pool.submit(hydrator, _job(source_company="source-b"), timeout_seconds=0.03)
        timed_out = waiter.result(1)
        assert timed_out["status"] == "timeout"
        assert timed_out["_detail_reuse"]["mode"] == "singleflight_timeout"
        assert timed_out["_detail_reuse"]["request_made"] is False
        assert timed_out["_detail_reuse"]["reused"] is False
        assert calls == 1
        release.set()
        assert leader.result(1)["status"] == "complete"

    assert calls == 1


def test_company_bound_response_is_not_cached_or_shared_across_source_labels() -> None:
    calls = 0

    def fetch(job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        result = _result()
        result["identity_evidence"] = (
            "native_id:7667551275686594821",
            "title:Platform Engineer",
            f"company:{job['source_company']}",
        )
        return result

    hydrator = ReusingDetailHydrator(fetch)
    first = hydrator(_job(source_company="source-a"), timeout_seconds=1)
    second = hydrator(_job(source_company="source-b"), timeout_seconds=1)

    assert calls == 2
    assert first["_detail_reuse"]["reason"] == "company_bound_response"
    assert second["_detail_reuse"]["reason"] == "company_bound_response"
    assert hydrator.snapshot()["cache_entries"] == 0


def test_byte_limit_and_entry_limit_bound_cache_memory() -> None:
    def fetch(job, *, timeout_seconds):
        return _result(
            url=job["detail_url"],
            post_id=job["detail_url"].split("/position/")[1].split("/")[0],
            detail="x" * 1500,
            identity_evidence=(
                f"native_id:{job['detail_url'].split('/position/')[1].split('/')[0]}",
                f"title:{job['title']}",
            ),
            capture_evidence=_capture(job["detail_url"], "x" * 1500),
        )

    hydrator = ReusingDetailHydrator(fetch, max_entries=4096, max_bytes=2500)
    for post_id in range(3):
        url = f"https://jobs.bytedance.com/campus/position/{post_id + 1}/detail"
        hydrator(_job(url, title=f"Role {post_id}"), timeout_seconds=1)

    snapshot = hydrator.snapshot()
    assert snapshot["cache_bytes"] <= 2500
    assert snapshot["cache_entries"] <= 4096
    assert DEFAULT_MAX_ENTRIES >= 4096


def test_unsupported_url_and_legacy_string_keep_their_return_types() -> None:
    calls = 0

    def fetch(job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        return "legacy-detail"

    hydrator = ReusingDetailHydrator(fetch)
    result = hydrator(
        {
            "title": "Role",
            "detail_url": "https://custom.example.com/position/1/detail",
        },
        timeout_seconds=1,
    )
    assert result == "legacy-detail"
    assert isinstance(result, str)
    assert calls == 1


def test_close_releases_run_cache_and_rejects_new_calls() -> None:
    hydrator = ReusingDetailHydrator(lambda _job, *, timeout_seconds: _result())
    hydrator(_job(), timeout_seconds=1)
    assert hydrator.snapshot()["cache_entries"] == 1
    hydrator.close()
    assert hydrator.snapshot()["cache_entries"] == 0
    assert hydrator.snapshot()["closed"] is True
    with pytest.raises(RuntimeError, match="closed"):
        hydrator(_job(), timeout_seconds=1)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timeout_is_rejected(timeout: float) -> None:
    hydrator = ReusingDetailHydrator(lambda _job, *, timeout_seconds: _result())
    with pytest.raises(ValueError, match="timeout_seconds"):
        hydrator(_job(), timeout_seconds=timeout)
