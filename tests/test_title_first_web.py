"""Static contract checks for the title-first company/source directory UI."""

from __future__ import annotations

import re
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1] / "apps" / "web"


def _read(name: str) -> str:
    return (WEB_ROOT / name).read_text(encoding="utf-8")


def test_all_web_assets_share_the_single_incremented_version() -> None:
    html = _read("index.html")
    assets = re.findall(r"(?:src|href)=\"\./(?:styles\.css|app\.js|company-sources\.js)\?v=([^\"]+)", html)
    assert assets == ["0.4.18-20260914"] * 3
    assert '<script defer src="./company-sources.js?v=0.4.18-20260914"></script>' in html


def test_navigation_and_company_dialog_layout_contract():
    html, js, css = _read("index.html"), _read("company-sources.js"), _read("styles.css")
    assert 'data-job-nav-mode="today"' not in html
    assert 'data-job-mode="today"' in html
    assert 'id="jobs-back-button"' in html
    assert 'company.dataset.companySourceDetail' in js
    assert 'dialog.showModal()' in js and 'dialog.close()' in js
    assert 'height: calc(100dvh - 104px)' in css


def test_company_page_exposes_source_directory_and_preserves_ranking_switch() -> None:
    html = _read("index.html")
    js = _read("company-sources.js")
    app = _read("app.js")

    assert 'data-view="companies"' in html
    assert "company-source-view-button" in js
    assert 'data-company-source-view="sources"' in js
    assert 'data-company-source-view="ranking"' in js
    assert "岗位排行" in js
    assert "/api/jobs/browse" in app
    assert "/api/jobs/browse" not in js


def test_source_directory_uses_source_registry_without_hiding_zero_job_failures() -> None:
    js = _read("company-sources.js")

    assert "/api/company-sources?" in js
    assert 'job_count' in js
    assert "来源记录加载中" in js
    assert "没有匹配的来源记录" in js
    assert "来源加载失败：" in js
    assert "无法抓取" in js
    assert 'value: "ungrabbable"' in js
    assert "failed" in js


def test_source_detail_reads_jobs_with_contract_fields_and_paging() -> None:
    js = _read("company-sources.js")

    assert "/api/company-sources/${encodeURIComponent(recordId)}/jobs?page=" in js
    for field in (
        "title",
        "detail_url",
        "capture_status",
        "capture_failure_reason",
        "availability_status",
        "match_score",
    ):
        assert f"job.{field}" in js
    assert "company-source-job-pager" in js
    assert "岗位列表读取失败：" in js
    assert "当前页暂无岗位记录" in js
    assert "第 ${page} / ${pages} 页" in js


def test_failed_jobs_keep_independent_capture_and_availability_states() -> None:
    js = _read("company-sources.js")
    css = _read("styles.css")

    assert 'capture_status || "unknown"' in js
    assert "availabilityStatusLabel" in js
    assert "company-source-job-status--failed" in css
    assert "company-source-job-availability--inactive" in css
    assert 'score === "未评分"' in js
    assert "未评分" in js
    assert "0 分" not in js


def test_source_links_are_safe_and_mobile_job_rows_do_not_require_horizontal_page_scroll() -> None:
    js = _read("company-sources.js")
    css = _read("styles.css")

    assert "safeHttpUrl" in js
    assert 'anchor.rel = "noopener noreferrer"' in js
    assert 'if (url.username || url.password) return null;' in js
    assert ".company-source-jobs-table thead { display: none; }" in css
    assert ".company-source-jobs-table td:nth-child(1)" in css
    assert ".company-source-jobs-table td:nth-child(2)" in css
    assert ".company-source-jobs-table td:nth-child(4)" in css
