import subprocess
from pathlib import Path

from packages.tools import (
    ConfiguredCrawlerRunInput,
    ConfiguredCrawlerRunner,
    ToolErrorCode,
    ToolStatus,
)
from packages.tools.crawler_run import RecruitmentCoreCrawlerProcess


def _result() -> dict:
    return {
        "company": "示例公司",
        "crawler_key": "moka",
        "configured_urls": ["https://jobs.example.com/campus"],
        "source_url": "https://jobs.example.com/campus",
        "allowed_origins": ["https://jobs.example.com"],
        "jobs": [
            {
                "id": "job-1",
                "title": "C++ 软件开发工程师",
                "city": "上海",
                "detail_url": "https://jobs.example.com/campus/job-1",
                "jd_raw": (
                    "职位描述：负责 Linux 平台 C++ 软件模块设计、开发和自动化测试。"
                    "任职要求：熟悉 C++、多线程、数据结构和软件工程实践，有完整项目经验。"
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "batch": "formal",
            }
        ],
        "raw_job_count": 1,
        "pages_seen": 3,
        "total_pages": 3,
        "has_more": False,
        "run_reason": "api_total_reached",
    }


def test_configured_runner_executes_and_audits_one_allowlisted_company(tmp_path) -> None:
    calls = []

    def process(source_root, request):
        calls.append((source_root, request.company))
        return _result()

    runner = ConfiguredCrawlerRunner(tmp_path, process)
    response = runner.run(
        ConfiguredCrawlerRunInput(company="示例公司", timeout_ms=30_000)
    )

    assert response.status is ToolStatus.SUCCESS
    assert response.success is True
    assert response.read_only is True
    assert response.data is not None
    assert response.data.crawler_key == "moka"
    assert response.data.accepted_count == 1
    assert response.data.pagination_complete is True
    assert response.data.pages_seen == 3
    assert calls == [(tmp_path.resolve(), "示例公司")]


def test_configured_runner_fails_closed_on_incomplete_pagination(tmp_path) -> None:
    payload = _result()
    payload.update(pages_seen=0, total_pages=3, has_more=True)
    runner = ConfiguredCrawlerRunner(tmp_path, lambda _root, _request: payload)

    response = runner.run(ConfiguredCrawlerRunInput(company="示例公司"))

    assert response.status is ToolStatus.FAILURE
    assert response.error_code is ToolErrorCode.PAGINATION_INCOMPLETE
    assert response.data is not None
    assert response.data.accepted_count == 1
    assert response.data.pagination_complete is False


def test_configured_runner_reports_timeout_without_partial_rows(tmp_path) -> None:
    def timeout(_root, _request):
        raise subprocess.TimeoutExpired(cmd=["fixed"], timeout=1)

    response = ConfiguredCrawlerRunner(tmp_path, timeout).run(
        ConfiguredCrawlerRunInput(company="示例公司")
    )

    assert response.status is ToolStatus.FAILURE
    assert response.error_code is ToolErrorCode.TIMEOUT
    assert response.timed_out is True
    assert response.data is None


def test_configured_runner_keeps_missing_pagination_evidence_unknown(tmp_path) -> None:
    payload = _result()
    payload.update(pages_seen=1, total_pages=None, completeness_known=False)
    response = ConfiguredCrawlerRunner(tmp_path, lambda *_args: payload).run(
        ConfiguredCrawlerRunInput(company="示例公司")
    )
    assert response.error_code is ToolErrorCode.PAGINATION_EVIDENCE_MISSING
    assert response.data.pagination_state == "unknown"
    assert not response.success


def test_agent_process_reads_companies_yaml_and_uses_recruitment_core(
    monkeypatch, tmp_path
) -> None:
    config = tmp_path / "companies.yaml"
    config.write_text(
        """
companies:
  - name: 示例公司
    careers_url: https://jobs.example.com/campus
    crawler: fake
""".strip(),
        encoding="utf-8",
    )

    from packages import recruitment_core

    monkeypatch.setattr(
        recruitment_core,
        "crawl_company_with_evidence",
        lambda company: {"pagination_complete": True, "completeness_known": True,
                         "pages_seen": 2, "total_pages": 2, "jobs": [
            {
                "id": "job-1",
                "title": "C++ 软件开发工程师",
                "city": "上海",
                "jd_url": "https://jobs.example.com/campus/job-1",
                "jd_raw": (
                    "职位描述：负责 Linux 平台 C++ 软件模块设计、开发和自动化测试。"
                    "任职要求：熟悉 C++、多线程、数据结构和软件工程实践，有完整项目经验。"
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "recruitment_track": "formal",
            }
        ]},
    )
    process = RecruitmentCoreCrawlerProcess(config)
    result = process(config, ConfiguredCrawlerRunInput(company="示例公司"))

    assert result["crawler_key"] == "fake"
    assert result["configured_urls"] == ["https://jobs.example.com/campus"]
    assert result["raw_job_count"] == 1
    assert result["pages_seen"] == result["total_pages"] == 2
    response = ConfiguredCrawlerRunner(config, process).run(
        ConfiguredCrawlerRunInput(company="示例公司")
    )
    assert response.status is ToolStatus.SUCCESS
    assert response.data is not None
    assert response.data.accepted_count == 1
    assert response.evidence[0].source_ref == "https://jobs.example.com/campus"


def test_agent_crawler_run_has_no_legacy_process_or_path_dependency() -> None:
    source = Path("packages/tools/crawler_run.py").read_text(encoding="utf-8")

    assert "packages.recruitment_core" in source
    assert "run_source_crawler.py" not in source
    assert "source_root" not in source
    assert "sys.path" not in source
    assert "accept_crawler_run" in source
    assert "ConfiguredCrawlerRunResponse" in source


def test_agent_process_does_not_allow_job_output_to_expand_trusted_origins(
    monkeypatch, tmp_path
) -> None:
    config = tmp_path / "companies.yaml"
    config.write_text(
        "companies:\n"
        "  - name: 示例公司\n"
        "    careers_url: https://jobs.example.com/campus\n"
        "    crawler: fake\n",
        encoding="utf-8",
    )
    from packages import recruitment_core

    monkeypatch.setattr(
        recruitment_core,
        "crawl_company_with_evidence",
        lambda _company: {"pagination_complete": True, "completeness_known": True,
                          "pages_seen": 1, "total_pages": 1, "jobs": [
            {
                "id": "job-evil",
                "title": "C++ 工程师",
                "jd_url": "https://evil.example/jobs/1",
                "jd_raw": (
                    "职位描述：负责 Linux 平台 C++ 软件模块设计、开发和自动化测试。"
                    "任职要求：熟悉 C++、多线程、数据结构和软件工程实践，有完整项目经验。"
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "recruitment_track": "formal",
            }
        ]},
    )

    process = RecruitmentCoreCrawlerProcess(config)
    payload = process(config, ConfiguredCrawlerRunInput(company="示例公司"))
    response = ConfiguredCrawlerRunner(config, process).run(
        ConfiguredCrawlerRunInput(company="示例公司")
    )

    assert payload["allowed_origins"] == ["https://jobs.example.com"]
    assert response.status is ToolStatus.NO_RESULTS
    assert response.data is not None
    assert response.data.rejection_reasons == {"detail_origin_not_allowed": 1}
