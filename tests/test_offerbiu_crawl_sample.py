from __future__ import annotations

import json

from scripts.eval_offerbiu_crawl_sample import (
    evaluate_sample,
    load_samples,
    preflight_url,
)


class FakeResponse:
    def __init__(self, status_code, *, headers=None, body=b"", encoding="utf-8"):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body
        self.encoding = encoding
        self.closed = False

    def iter_content(self, chunk_size=65536):
        del chunk_size
        yield self.body

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.cookies = {}
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    def close(self):
        pass


def test_preflight_checks_each_public_redirect_and_does_not_call_200_success():
    session = FakeSession(
        [
            FakeResponse(302, headers={"Location": "/campus/jobs"}),
            FakeResponse(
                200,
                headers={"Content-Type": "text/html"},
                body=b"<html><head><title>Campus Jobs</title></head>"
                b"<body>Recruitment position list Apply</body></html>",
            ),
        ]
    )

    result = preflight_url("https://jobs.example.test/start", session=session)

    assert result["preflight_status"] == "checked"
    assert result["allow_crawl"] is True
    assert result["page_kind"] == "job_listing_possible"
    assert result["final_url"] == "https://jobs.example.test/campus/jobs"
    assert len(result["redirects"]) == 1
    assert all(kwargs["allow_redirects"] is False for _, kwargs in session.calls)


def test_preflight_skips_private_and_credential_urls_without_requests():
    session = FakeSession([])

    private = preflight_url("http://127.0.0.1/jobs", session=session)
    credential = preflight_url("https://user:pass@example.com/jobs", session=session)

    assert private["preflight_status"] == "skip"
    assert credential["preflight_status"] == "skip"
    assert session.calls == []


def test_js_shell_remains_unknown_and_is_not_reported_as_no_jobs():
    session = FakeSession(
        [
            FakeResponse(
                200,
                headers={"Content-Type": "text/html"},
                body=b'<html><body><div id="app"></div><script src="app.js"></script></body></html>',
            )
        ]
    )

    result = preflight_url("https://jobs.example.test/", session=session)

    assert result["page_kind"] == "unknown"
    assert result["js_shell"] is True
    assert "no_jobs" not in result["reason"]


def test_anonymous_redirect_keeps_server_cookie_and_spa_route():
    class CookieSession(FakeSession):
        def get(self, url, **kwargs):
            assert url.endswith("#/jobs?project=42")
            if self.calls:
                assert self.cookies == {"anonymous_session": "fresh"}
            else:
                assert not self.cookies
                self.cookies["anonymous_session"] = "fresh"
            return super().get(url, **kwargs)

    session = CookieSession([
        FakeResponse(302, headers={"Location": "/campus"}),
        FakeResponse(200, body=b"<html><body>Campus Jobs</body></html>"),
    ])
    result = preflight_url("https://jobs.example.test/campus#/jobs?project=42", session=session)
    assert result["preflight_status"] == "checked"
    assert result["final_url"].endswith("#/jobs?project=42")
    assert not session.cookies


def test_admin_edit_entry_is_not_sent_to_crawler():
    session = FakeSession([])
    result = preflight_url("https://admin.example.test/announcements/123/edit", session=session)
    assert result["reason"] == "non_recruitment_admin_entry"
    assert result["allow_crawl"] is False
    assert not session.calls


def test_weak_captcha_text_does_not_block_public_page():
    session = FakeSession(
        [
            FakeResponse(
                200,
                headers={"Content-Type": "text/html"},
                body=b"<html><body>Campus jobs <div class='login-modal'>captcha</div></body></html>",
            )
        ]
    )

    result = preflight_url("https://jobs.example.test/jobs", session=session)

    assert result["preflight_status"] == "checked"
    assert result["allow_crawl"] is True
    assert "potential_access_restriction" in result["signals"]


def test_sample_uses_existing_adapter_and_reports_raw_jd_statistics():
    calls = []

    def fake_probe(url, **kwargs):
        assert url == "https://acme.zhiye.com/campus/jobs"
        assert kwargs["budget_seconds"] <= 15
        return {
            "preflight_status": "checked",
            "allow_crawl": True,
            "final_url": url,
            "page_kind": "job_listing_possible",
        }

    def fake_process(**kwargs):
        calls.append(kwargs)
        return {
            "jobs": [
                {
                    "id": "complete",
                    "title": "机器人软件工程师",
                    "jd_raw": (
                        "1. 负责机器人控制软件的模块设计、核心代码开发与持续维护；"
                        "2. 参与传感器、执行器和规划模块的接口设计，完成联调测试、性能分析及故障定位；"
                        "3. 构建自动化测试与发布流程，沉淀可复用工具，支持产品在不同硬件平台上的部署；"
                        "4. 与算法、硬件和产品团队协作，评审技术方案，推进关键功能按计划交付并持续改进；"
                        "5. 熟悉 C++、Python、Linux 和常用数据结构，具备良好的编码规范与工程实践能力；"
                        "6. 能够阅读英文技术资料，掌握多线程或网络编程，有机器人项目经验者优先，并具备清晰沟通能力。"
                    ),
                },
                {"id": "partial", "title": "软件工程师", "jd_raw": ""},
            ],
            "pagination_complete": False,
            "completeness_known": True,
            "pages_seen": 1,
            "total_pages": 2,
            "has_more": True,
            "advertised_total": 3,
        }

    result = evaluate_sample(
        {
            "companyName": "测试公司",
            "applyUrl": "https://acme.zhiye.com/campus/jobs",
            "industryGroupCodes": ["internet-tech"],
            "id": "offer-1",
        },
        timeout_seconds=10,
        preflight=fake_probe,
        process=fake_process,
    )

    assert result["crawler_key"] == "beisen"
    assert result["crawl"]["raw_job_count"] == 2
    assert result["crawl"]["complete_jd_count"] == 0
    assert result["crawl"]["incomplete_jd_count"] == 1
    assert result["crawl"]["unknown_jd_count"] == 1
    assert result["crawl"]["jd_check"] == "official_capture_evidence_v1"
    assert result["crawl"]["pagination_evidence"]["has_more"] is True
    assert result["formal_acceptance"] == "not_run"
    assert result["db_writes"] == 0
    assert calls[0]["source_context"]["source_cohort_source"] == "offerbiu"
    assert "OC" not in calls[0]["source_context"]["source_cohort_evidence"]


def test_load_samples_accepts_list_and_samples_object(tmp_path):
    rows = [
        {
            "companyName": "A",
            "applyUrl": "https://a.example/jobs",
            "industryGroupCodes": [],
            "id": "1",
        }
    ]
    list_path = tmp_path / "list.json"
    object_path = tmp_path / "object.json"
    list_path.write_text(json.dumps(rows), encoding="utf-8")
    object_path.write_text(json.dumps({"samples": rows}), encoding="utf-8")

    assert load_samples(list_path)[0]["companyName"] == "A"
    assert load_samples(object_path)[0]["companyName"] == "A"


def test_load_samples_accepts_selected_companies_and_selects_one_entry_per_company(tmp_path):
    path = tmp_path / "selected.json"
    path.write_text(
        json.dumps(
            {
                "selected_companies": [
                    {
                        "company_key": "id:a",
                        "company_name": "A",
                        "records": [
                            {
                                "id": "wechat",
                                "classification": "wechat_article",
                                "raw_record": {
                                    "companyName": "A",
                                    "applyUrl": "https://mp.weixin.qq.com/s/article",
                                    "industryGroupCodes": ["internet-tech"],
                                },
                            },
                            {
                                "id": "ats",
                                "url_family": "known_ats",
                                "raw_record": {
                                    "companyName": "A",
                                    "applyUrl": "https://acme.zhiye.com/campus/jobs",
                                    "industryGroupCodes": ["internet-tech"],
                                },
                            },
                        ],
                    }
                ],
                "selected_records": [],
            }
        ),
        encoding="utf-8",
    )

    selected = load_samples(path)

    assert len(selected) == 1
    assert selected[0]["applyUrl"] == "https://acme.zhiye.com/campus/jobs"
    assert selected[0]["_selection"]["other_entry_count"] == 1
    assert selected[0]["_selection"]["other_entries_verified"] is False
