from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import requests


def _load_adapter(module_name: str):
    """Load one adapter without importing the registry's unrelated renderers."""
    package_name = "_platform_completeness_crawlers"
    crawler_dir = Path(__file__).parents[1] / "packages" / "recruitment_core" / "crawlers"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(crawler_dir)]
        sys.modules[package_name] = package

    base_name = f"{package_name}.base"
    if base_name not in sys.modules:
        base_spec = importlib.util.spec_from_file_location(base_name, crawler_dir / "base.py")
        base_module = importlib.util.module_from_spec(base_spec)
        sys.modules[base_name] = base_module
        base_spec.loader.exec_module(base_module)

    full_name = f"{package_name}.{module_name}"
    spec = importlib.util.spec_from_file_location(full_name, crawler_dir / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


alibaba_module = _load_adapter("alibaba")
cvte_module = _load_adapter("cvte")
pdd_module = _load_adapter("pdd")
lenovo_module = _load_adapter("lenovo")
bilibili_module = _load_adapter("bilibili")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return copy.deepcopy(self.payload)


def test_lenovo_reports_complete_api_pagination(monkeypatch) -> None:
    pages = {
        1: {
            "result": {
                "rows": [{"id": "lenovo-1", "jobName": "软件工程师"}],
                "total": 2,
                "pageSize": 1,
            }
        },
        2: {
            "result": {
                "rows": [{"id": "lenovo-2", "jobName": "算法工程师"}],
                "total": 2,
                "pageSize": 1,
            }
        },
    }

    def fake_get(_url, *, params, **_kwargs):
        return _Response(pages[params["pageNum"]])

    monkeypatch.setattr(lenovo_module.requests, "get", fake_get)
    crawler = lenovo_module.LenovoCrawler(
        "联想", "https://talent.lenovo.com.cn/position"
    )
    crawler.PAGE_SIZE = 1

    assert len(crawler.fetch()) == 2
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 2
    assert crawler.total_pages == 2
    assert crawler.advertised_total == 2
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "advertised_total_reached"


def test_lenovo_request_failure_preserves_incomplete_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        lenovo_module.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.Timeout("fixture")),
    )
    crawler = lenovo_module.LenovoCrawler(
        "联想", "https://talent.lenovo.com.cn/position"
    )

    assert crawler.fetch() == []
    assert crawler.pagination_complete is False
    assert crawler.fetch_failed is True
    assert crawler.pagination_termination_reason == "page_request_failed"


def test_bilibili_api_total_proves_complete_pagination() -> None:
    crawler = bilibili_module.BilibiliCrawler(
        "bilibili", "https://jobs.bilibili.com/campus/positions"
    )
    crawler._reset_pagination_evidence()
    jobs, seen = [], set()
    payload = {
        "data": {
            "list": [
                {"id": "bili-1", "positionName": "后端开发工程师"},
                {"id": "bili-2", "positionName": "算法工程师"},
            ],
            "totalCount": 2,
            "currentPage": 1,
            "pageSize": 10,
        }
    }

    assert crawler._parse_api_payload(payload, jobs, seen) == 2
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 1
    assert crawler.total_pages == 1
    assert crawler.advertised_total == 2
    assert crawler.pagination_termination_reason == "api_total_reached"


CVTE_COMPLETE = json.loads(
    """
    {
      "projectPositions": [
        {
          "id": "cvte-a-1",
          "projectId": "project-a",
          "name": "嵌入式软件工程师",
          "areaViews": [{"cityName": "广州"}],
          "duty": "岗位职责：" ,
          "requirement": "任职要求：熟悉 C/C++。"
        }
      ],
      "total": 1
    }
    """
)
CVTE_COMPLETE["projectPositions"][0]["duty"] = "岗位职责：" + "负责嵌入式软件开发。" * 120


def test_cvte_reports_scoped_complete_evidence_and_keeps_long_jd(monkeypatch) -> None:
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(CVTE_COMPLETE)

    monkeypatch.setattr(cvte_module.requests, "get", fake_get)
    crawler = cvte_module.CVTECrawler(
        "CVTE",
        "https://campus.cvte.com/project/project-a?entry=overseas",
    )

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert calls[0][0] == crawler.POSITIONS_API
    assert calls[0][1]["params"] == {"projectIds": "project-a"}
    assert len(jobs[0]["jd_raw"]) > 500
    assert len(jobs[0]["jd_raw"]) <= 12000
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 1
    assert crawler.total_pages == 1
    assert crawler.advertised_total == 1
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "api_total_reached"
    assert crawler.fetch_failed is False


def test_cvte_keeps_project_scope_and_root_discovery(monkeypatch) -> None:
    payload = {
        "projectPositions": [
            {"id": "cvte-a-1", "projectId": "project-a", "name": "算法工程师"},
            {"id": "cvte-a-1", "projectId": "project-a", "name": "算法工程师"},
            {"id": "cvte-b-1", "projectId": "project-b", "name": "后端工程师"},
        ],
        "total": 2,
    }
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == cvte_module.CVTECrawler.PROJECTS_API:
            return _Response({"projects": [{"id": "project-a"}, {"id": "project-b"}]})
        return _Response(payload)

    monkeypatch.setattr(cvte_module.requests, "get", fake_get)
    scoped = cvte_module.CVTECrawler(
        "CVTE",
        "https://campus.cvte.com/project/project-a",
    )
    jobs = scoped.fetch()

    assert [job["title"] for job in jobs] == ["算法工程师"]
    assert scoped.pagination_complete is False
    assert scoped.pagination_termination_reason == "project_scope_mismatch"
    assert scoped.fetch_failed is False

    root_payloads = {
        "project-a": {
            "projectPositions": [
                {"id": "cvte-a-1", "projectId": "project-a", "name": "算法工程师"},
            ],
            "total": 1,
        },
        "project-b": {
            "projectPositions": [
                {"id": "cvte-b-1", "projectId": "project-b", "name": "后端工程师"},
            ],
            "total": 1,
        },
    }

    def root_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == cvte_module.CVTECrawler.PROJECTS_API:
            return _Response({"projects": [{"id": "project-a"}, {"id": "project-b"}]})
        return _Response(root_payloads[kwargs["params"]["projectIds"]])

    monkeypatch.setattr(cvte_module.requests, "get", root_get)
    root = cvte_module.CVTECrawler("CVTE", "https://campus.cvte.com/position")
    root_jobs = root.fetch()

    assert [job["title"] for job in root_jobs] == ["算法工程师", "后端工程师"]
    assert root.pagination_complete is True
    assert root.advertised_total is None
    assert root.pages_seen == 2
    assert root.total_pages == 2
    assert root.pagination_termination_reason == "all_active_projects_complete"
    assert [item["advertised_total"] for item in root.project_diagnostics] == [1, 1]
    assert [item["observed_unique"] for item in root.project_diagnostics] == [1, 1]
    assert [call[0] for call in calls[-3:]] == [
        cvte_module.CVTECrawler.PROJECTS_API,
        cvte_module.CVTECrawler.POSITIONS_API,
        cvte_module.CVTECrawler.POSITIONS_API,
    ]
    assert [call[1]["params"] for call in calls[-2:]] == [
        {"projectIds": "project-a"},
        {"projectIds": "project-b"},
    ]


def test_cvte_root_allows_cross_project_overlap_after_independent_completion(monkeypatch) -> None:
    payloads = {
        "project-a": {
            "projectPositions": [
                {"id": "cvte-shared-1", "projectId": "project-a", "name": "共享岗位"},
            ],
            "total": 1,
        },
        "project-b": {
            "projectPositions": [
                {"id": "cvte-shared-1", "projectId": "project-b", "name": "共享岗位"},
            ],
            "total": 1,
        },
    }

    def fake_get(url, **kwargs):
        if url == cvte_module.CVTECrawler.PROJECTS_API:
            return _Response({"projects": [{"id": "project-a"}, {"id": "project-b"}]})
        return _Response(payloads[kwargs["params"]["projectIds"]])

    monkeypatch.setattr(cvte_module.requests, "get", fake_get)
    crawler = cvte_module.CVTECrawler("CVTE", "https://campus.cvte.com/position")

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert crawler.pagination_complete is True
    assert crawler.advertised_total is None
    assert crawler.pages_seen == 2
    assert crawler.total_pages == 2
    assert crawler.has_more is False
    assert crawler.project_diagnostics[0]["pagination_complete"] is True
    assert crawler.project_diagnostics[1]["pagination_complete"] is True
    assert [item["advertised_total"] for item in crawler.project_diagnostics] == [1, 1]


def test_cvte_project_internal_duplicate_remains_incomplete(monkeypatch) -> None:
    def fake_get(_url, **_kwargs):
        return _Response({
            "projectPositions": [
                {"id": "cvte-dup-1", "projectId": "project-a", "name": "算法工程师"},
                {"id": "cvte-dup-1", "projectId": "project-a", "name": "算法工程师"},
            ],
            "total": 2,
        })

    monkeypatch.setattr(cvte_module.requests, "get", fake_get)
    crawler = cvte_module.CVTECrawler(
        "CVTE",
        "https://campus.cvte.com/project/project-a",
    )

    assert len(crawler.fetch()) == 1
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "duplicate_job_id"
    assert crawler.project_diagnostics[0]["pagination_complete"] is False


def test_cvte_unpaginated_contract_does_not_infer_official_total(monkeypatch) -> None:
    def fake_get(_url, **_kwargs):
        return _Response({
            "projectPositions": [
                {"id": "cvte-no-total", "projectId": "project-a", "name": "软件工程师"},
            ],
        })

    monkeypatch.setattr(cvte_module.requests, "get", fake_get)
    crawler = cvte_module.CVTECrawler(
        "CVTE",
        "https://campus.cvte.com/project/project-a",
    )

    assert len(crawler.fetch()) == 1
    assert crawler.pagination_complete is True
    assert crawler.advertised_total is None
    assert crawler.project_diagnostics[0]["advertised_total"] is None
    assert crawler.pagination_termination_reason == "api_total_reached"


def test_cvte_root_aggregates_project_failure_without_reporting_complete(monkeypatch) -> None:
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == cvte_module.CVTECrawler.PROJECTS_API:
            return _Response({"projects": [{"id": "project-a"}, {"id": "project-b"}]})
        if kwargs["params"] == {"projectIds": "project-a"}:
            return _Response({
                "projectPositions": [{"id": "cvte-a-1", "projectId": "project-a", "name": "算法工程师"}],
                "total": 1,
            })
        raise requests.Timeout("project-b fixture failure")

    monkeypatch.setattr(cvte_module.requests, "get", fake_get)
    crawler = cvte_module.CVTECrawler("CVTE", "https://campus.cvte.com/position")

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert crawler.pagination_complete is False
    assert crawler.pages_seen == 1
    assert crawler.total_pages is None
    assert crawler.advertised_total is None
    assert crawler.pagination_termination_reason == "positions_request_failed"
    assert crawler.fetch_failed is True


def test_cvte_marks_request_failure_incomplete(monkeypatch) -> None:
    def fake_get(_url, **_kwargs):
        raise requests.Timeout("offline fixture failure")

    monkeypatch.setattr(cvte_module.requests, "get", fake_get)
    crawler = cvte_module.CVTECrawler(
        "CVTE",
        "https://campus.cvte.com/project/project-a",
    )

    assert crawler.fetch() == []
    assert crawler.pagination_complete is False
    assert crawler.pages_seen == 0
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "positions_request_failed"
    assert crawler.fetch_failed is True


def test_cvte_recovers_invalid_project_from_observed_page_scope(monkeypatch) -> None:
    old_project_id = "7304f092b3cc11f083a91070fd64d1d0"
    current_project_id = "d39828c70ff94d37b9eb6521f6706692"
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == cvte_module.CVTECrawler.POSITIONS_API:
            project_id = kwargs["params"]["projectIds"]
            if project_id == old_project_id:
                return _Response({"status": 400, "msg": "无此项目"})
            return _Response({
                "projectPositions": [{
                    "id": "cvte-current-1",
                    "projectId": current_project_id,
                    "name": "软件工程师",
                }],
                "total": 1,
            })
        if url == f"{cvte_module.CVTECrawler.PROJECTS_API}/{old_project_id}":
            return _Response({"status": 400, "msg": "无此项目"})
        if url == cvte_module.CVTECrawler.PROJECTS_API:
            return _Response({"projects": [{
                "id": current_project_id,
                "name": "CVTE2027届秋季校园招聘",
            }]})
        raise AssertionError(f"unexpected CVTE URL: {url}")

    monkeypatch.setattr(cvte_module.requests, "get", fake_get)
    crawler = cvte_module.CVTECrawler(
        "CVTE",
        f"https://campus.cvte.com/project/{old_project_id}?entry=overseas",
    )
    monkeypatch.setattr(
        crawler,
        "_observe_page_scope",
        lambda: {
            "status": "observed",
            "reason": "position_scope_observed",
            "project_ids": [current_project_id],
            "request_url": (
                "https://campus.cvte.com/api/position?projectIds="
                f"{current_project_id}"
            ),
            "project_name": "",
        },
    )

    jobs = crawler.fetch()

    assert [job["title"] for job in jobs] == ["软件工程师"]
    assert jobs[0]["job_type"] == "校招"
    assert crawler.pagination_complete is True
    assert crawler.scope_changed is True
    assert crawler.scope_old_project_id == old_project_id
    assert crawler.scope_observed_project_id == current_project_id
    assert crawler.scope_observed_project_name == "CVTE2027届秋季校园招聘"
    assert crawler.scope_observation_status == "changed"
    assert crawler.project_diagnostics[0]["project_id"] == current_project_id
    assert crawler.project_diagnostics[0]["old_project_id"] == old_project_id
    assert crawler.project_diagnostics[0]["observed_project_name"] == (
        "CVTE2027届秋季校园招聘"
    )
    assert [call[1].get("params", {}).get("projectIds") for call in calls
            if call[0] == cvte_module.CVTECrawler.POSITIONS_API] == [
                old_project_id,
                current_project_id,
            ]


def test_cvte_rejects_invalid_project_without_observed_page_scope(monkeypatch) -> None:
    old_project_id = "legacy-project"
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == cvte_module.CVTECrawler.POSITIONS_API:
            return _Response({"status": 400, "msg": "无此项目"})
        if url == f"{cvte_module.CVTECrawler.PROJECTS_API}/{old_project_id}":
            return _Response({"status": 400, "msg": "无此项目"})
        raise AssertionError(f"unexpected CVTE URL: {url}")

    monkeypatch.setattr(cvte_module.requests, "get", fake_get)
    crawler = cvte_module.CVTECrawler(
        "CVTE",
        f"https://campus.cvte.com/project/{old_project_id}",
    )
    monkeypatch.setattr(
        crawler,
        "_observe_page_scope",
        lambda: {
            "status": "unknown",
            "reason": "scope_observation_missing",
            "project_ids": [],
            "request_url": "",
            "project_name": "",
        },
    )

    assert crawler.fetch() == []
    assert crawler.pagination_complete is False
    assert crawler.has_more is True
    assert crawler.fetch_failed is False
    assert crawler.pagination_termination_reason == "scope_observation_missing"
    assert crawler.scope_changed is False
    assert crawler.scope_observation_status == "unknown"
    assert crawler.scope_old_project_id == old_project_id
    assert all(call[0] != cvte_module.CVTECrawler.POSITIONS_API for call in calls[1:])


def test_cvte_scope_listener_accepts_only_exact_position_endpoint() -> None:
    parse_scope = cvte_module.CVTECrawler._position_scope_from_url

    assert parse_scope(
        "https://campus.cvte.com/api/position?projectIds=current-project"
    )["project_ids"] == ["current-project"]
    assert parse_scope(
        "https://campus.cvte.com/api/position/extra?projectIds=current-project"
    )["project_ids"] == []
    assert parse_scope(
        "https://assets.campus.cvte.com/api/position?projectIds=current-project"
    )["project_ids"] == []


ALIBABA_PAGE_1 = {
    "content": {
        "datas": [{"id": "ali-1", "name": "基础架构工程师", "workLocations": ["杭州"]}],
        "totalCount": 2,
        "pageSize": 1,
        "totalPages": 2,
        "currentPage": 1,
    }
}
ALIBABA_PAGE_2 = {
    "content": {
        "datas": [{"id": "ali-2", "name": "存储研发工程师", "workLocations": ["杭州"]}],
        "totalCount": 2,
        "pageSize": 1,
        "totalPages": 2,
        "currentPage": 2,
    }
}


class _AlibabaSession:
    def __init__(self, responses):
        self.responses = responses
        self.headers = {}
        self.cookies = {"XSRF-TOKEN": "csrf-fixture"}
        self.calls = []

    def get(self, _url, **_kwargs):
        return _Response({})

    def post(self, _url, *, json, **_kwargs):
        self.calls.append(copy.deepcopy(json))
        response = self.responses[json["pageIndex"]]
        if isinstance(response, BaseException):
            raise response
        return _Response(response)


def _run_alibaba(monkeypatch, responses, *, max_pages=None):
    session = _AlibabaSession(responses)
    monkeypatch.setattr(alibaba_module.requests, "Session", lambda: session)
    crawler = alibaba_module.AlibabaCrawler(
        "Token Foundry",
        "https://campus-talent.alibaba.com/campus/position?"
        "batchId=100000760001&filterParams=customDeptQ5EOVE&circleCode=80266",
    )
    crawler.PAGE_SIZE = 1
    if max_pages is not None:
        crawler.MAX_PAGES = max_pages
    jobs = crawler.fetch()
    return crawler, jobs, session


def test_alibaba_preserves_batch_department_filters_and_reports_complete(monkeypatch) -> None:
    crawler, jobs, session = _run_alibaba(
        monkeypatch,
        {1: ALIBABA_PAGE_1, 2: ALIBABA_PAGE_2},
    )

    assert len(jobs) == 2
    assert [call["batchId"] for call in session.calls] == [100000760001, 100000760001]
    assert all(call["customDeptCode"] == "Q5EOVE" for call in session.calls)
    assert all(call["referralCircleCode"] == "80266" for call in session.calls)
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 2
    assert crawler.total_pages == 2
    assert crawler.advertised_total == 2
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "api_total_reached"
    assert crawler.fetch_failed is False


def test_alibaba_middle_page_failure_is_not_complete(monkeypatch) -> None:
    crawler, jobs, _session = _run_alibaba(
        monkeypatch,
        {1: ALIBABA_PAGE_1, 2: requests.Timeout("page 2 failed")},
    )

    assert len(jobs) == 1
    assert crawler.pagination_complete is False
    assert crawler.pages_seen == 1
    assert crawler.total_pages == 2
    assert crawler.advertised_total == 2
    assert crawler.has_more is True
    assert crawler.pagination_termination_reason == "page_request_failed"
    assert crawler.fetch_failed is True


def test_alibaba_rejects_page_limit_and_total_changes(monkeypatch) -> None:
    limited, _, _ = _run_alibaba(
        monkeypatch,
        {1: {"content": {"datas": [{"id": "ali-1", "name": "工程师"}],
                           "totalCount": 3, "pageSize": 1, "totalPages": 3}}},
        max_pages=1,
    )
    assert limited.pagination_complete is False
    assert limited.has_more is True
    assert limited.pagination_termination_reason == "max_pages_reached"

    changed, _, _ = _run_alibaba(
        monkeypatch,
        {
            1: ALIBABA_PAGE_1,
            2: {
                "content": {
                    "datas": [{"id": "ali-2", "name": "存储研发工程师"}],
                    "totalCount": 3,
                    "pageSize": 1,
                    "totalPages": 3,
                    "currentPage": 2,
                }
            },
        },
    )
    assert changed.pagination_complete is False
    assert changed.has_more is True
    assert changed.pagination_termination_reason == "total_changed_between_pages"
    assert changed.fetch_failed is False


PDD_DETAIL = {
    "pdd-1": {"jobDuty": "负责平台研发。", "serveRequirement": "熟悉工程实践。"},
    "pdd-2": {"jobDuty": "负责数据研发。", "serveRequirement": "熟悉数据结构。"},
}


class _PDDSession:
    def __init__(self, list_responses, details=None):
        self.list_responses = list_responses
        self.details = details or PDD_DETAIL
        self.headers = {}
        self.calls = []

    def post(self, url, *, json, **_kwargs):
        self.calls.append((url, copy.deepcopy(json)))
        if url.endswith("/list"):
            page = json.get("page") or json.get("pageNo")
            response = self.list_responses[page]
        else:
            response = self.details.get(str(json["id"]))
        if isinstance(response, BaseException):
            raise response
        return _Response({"success": True, "result": response})


def _run_pdd(monkeypatch, list_responses, *, max_pages=None, details=None, page_size=1):
    session = _PDDSession(list_responses, details)
    monkeypatch.setattr(pdd_module.requests, "Session", lambda: session)
    crawler = pdd_module.PDDCrawler("拼多多", "https://careers.pddglobalhr.com/campus/grad")
    crawler.PAGE_SIZE = page_size
    if max_pages is not None:
        crawler.MAX_PAGES = max_pages
    jobs = crawler.fetch()
    return crawler, jobs, session


def test_pdd_accepts_stable_api_total(monkeypatch) -> None:
    crawler, jobs, _session = _run_pdd(
        monkeypatch,
        {
            1: {"list": [{"id": "pdd-1", "name": "后端工程师"}], "total": 2, "pageSize": 1},
            2: {"list": [{"id": "pdd-2", "name": "数据工程师"}], "total": 2, "pageSize": 1},
        },
    )

    assert len(jobs) == 2
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 2
    assert crawler.total_pages == 2
    assert crawler.advertised_total == 2
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "api_total_reached"
    assert crawler.fetch_failed is False


def test_pdd_uses_server_page_size_when_request_is_capped(monkeypatch) -> None:
    rows = [
        {"id": f"pdd-cap-{index}", "name": f"岗位 {index}"}
        for index in range(29)
    ]
    list_responses = {
        page: {
            "list": rows[(page - 1) * 10:page * 10],
            "total": 29,
            "pageNo": page,
        }
        for page in range(1, 4)
    }

    crawler, jobs, session = _run_pdd(
        monkeypatch,
        list_responses,
        page_size=100,
    )

    list_calls = [body for url, body in session.calls if url.endswith("/list")]
    assert len(jobs) == 29
    assert [body["page"] for body in list_calls] == [1, 2, 3]
    assert all(body["pageSize"] == 100 for body in list_calls)
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 3
    assert crawler.total_pages == 3
    assert crawler.advertised_total == 29
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "api_total_reached"
    assert crawler.fetch_failed is False


def test_pdd_duplicate_total_change_and_limit_are_incomplete(monkeypatch) -> None:
    duplicate, jobs, _ = _run_pdd(
        monkeypatch,
        {
            1: {"list": [{"id": "pdd-1", "name": "后端工程师"}], "total": 2, "pageSize": 1},
            2: {"list": [{"id": "pdd-1", "name": "后端工程师"}], "total": 2, "pageSize": 1},
        },
    )
    assert len(jobs) == 1
    assert duplicate.pagination_complete is False
    assert duplicate.pagination_termination_reason == "duplicate_job_id"

    changed, _, _ = _run_pdd(
        monkeypatch,
        {
            1: {"list": [{"id": "pdd-1", "name": "后端工程师"}], "total": 2, "pageSize": 1},
            2: {"list": [{"id": "pdd-2", "name": "数据工程师"}], "total": 3, "pageSize": 1},
        },
    )
    assert changed.pagination_complete is False
    assert changed.pagination_termination_reason == "total_changed_between_pages"
    assert changed.has_more is True

    limited, _, _ = _run_pdd(
        monkeypatch,
        {
            1: {"list": [{"id": "pdd-1", "name": "后端工程师"}], "total": 3, "pageSize": 1},
        },
        max_pages=1,
    )
    assert limited.pagination_complete is False
    assert limited.pagination_termination_reason == "max_pages_reached"
    assert limited.has_more is True


def test_pdd_requires_total_or_explicit_unpaginated_contract(monkeypatch) -> None:
    contract, jobs, _ = _run_pdd(
        monkeypatch,
        {1: {"list": [{"id": "pdd-1", "name": "后端工程师"}], "pagination": False}},
    )
    assert len(jobs) == 1
    assert contract.pagination_complete is True
    assert contract.pages_seen == 1
    assert contract.total_pages == 1
    assert contract.advertised_total == 1
    assert contract.has_more is False
    assert contract.pagination_termination_reason == "explicit_no_pagination_contract"

    unknown, _, _ = _run_pdd(
        monkeypatch,
        {1: {"list": [{"id": "pdd-1", "name": "后端工程师"}]}},
    )
    assert unknown.pagination_complete is False
    assert unknown.pagination_termination_reason == "missing_total"
