from __future__ import annotations

from pathlib import Path

from packages.recruitment_core.beisen_legacy_detail import (
    parse_beisen_legacy_detail,
)


CSSC_URL = "https://cssc.zhiye.com/zpdetail/311170879?PageIndex=4"
SAST_URL = "https://sast.zhiye.com/zpdetail/511201819?PageIndex=7"


CSSC_HTML = """
<!doctype html>
<html><head><title>中船集团网申系统--招聘详细</title></head><body>
  <header>首页 社会招聘 校园招聘 实习生招聘</header>
  <main class="STJobDetailLayout">
    <article class="STJobDetailMain">
      <div class="STJobTitle"><span>软件研发工程师(J12031)</span><em>（未申请）</em></div>
      <div class="STJobMeta"><span>招聘类别：</span><span>校园招聘</span></div>
      <section class="STJobDetailContent">
        <h3>工作职责：</h3>
        <div class="STJobDescription"><p>软件研发设计相关工作。</p></div>
        <h3>任职资格：</h3>
        <div class="STJobDescription"><ol>
          <li>计算机科学与技术、软件工程、人工智能等相关专业。</li>
          <li>硕士研究生及以上学历，2027届应届毕业生。</li>
        </ol></div>
        <a class="apply" href="/apply/311170879">现在申请</a>
      </section>
    </article>
    <aside class="STJobDetailRight">
      <h3>热招职位</h3>
      <a href="/zpdetail/311170870">智能认知算法工程师(J12000)</a>
      <h3>长招职位</h3>
      <a href="/zpdetail/311170869">软件工程师(J11999)</a>
    </aside>
  </main>
  <footer>©2026 中船集团</footer>
</body></html>
"""


SAST_HTML = """
<html><body>
  <div class="detail-page">
    <h1 class="STJobTitle">化学电源研究师（AI人工智能方向）（2027校招） <span>(未申请)</span></h1>
    <div class="STJobDetailContent">
      <div class="section-title">岗位职责</div>
      <p>要求博士学历，材料、化学、机械、智能科学与技术、人工智能等专业优先考虑。</p>
      <div class="section-title">任职要求</div>
      <p>熟悉相关方向国内外技术现状；具备基本的英语读、写、听、说能力。</p>
    </div>
    <div class="sidebar">
      <h3>热门职位</h3>
      <a href="/zpdetail/511201800">大模型算法工程师-2027</a>
    </div>
</body></html>
"""


def test_cssc_legacy_page_is_bounded_to_primary_detail_and_binds_route_title():
    result = parse_beisen_legacy_detail(
        CSSC_HTML,
        url=CSSC_URL,
        expected_title="软件研发工程师(J12031)",
        expected_job_id="311170879",
    )

    assert result.status == "complete"
    assert result.job_id == "311170879"
    assert result.title == "软件研发工程师(J12031)"
    assert result.application_label == "未申请"
    assert "工作职责" in result.body
    assert "任职资格" in result.body
    assert "软件研发设计相关工作" in result.body
    assert "2027届应届毕业生" in result.body
    assert "智能认知算法工程师" not in result.body
    assert "软件工程师(J11999)" not in result.body
    assert result.diagnostics["observed_job_ids"] == []
    assert {item["section"] for item in result.body_evidence} == {
        "responsibilities",
        "requirements",
    }


def test_sast_short_body_and_doctoral_text_are_preserved_without_title_policy():
    result = parse_beisen_legacy_detail(
        SAST_HTML,
        url=SAST_URL,
        expected_title="化学电源研究师（AI人工智能方向）（2027校招）",
        expected_job_id="511201819",
    )

    assert result.status == "complete"
    assert result.title == "化学电源研究师（AI人工智能方向）（2027校招）"
    assert result.application_label == "未申请"
    assert "博士学历" in result.body
    assert "国内外技术现状" in result.body
    assert result.diagnostics["body_char_count"] < 300


def test_modern_jobad_route_is_not_applicable_to_legacy_parser():
    result = parse_beisen_legacy_detail(
        CSSC_HTML,
        url="https://example.zhiye.com/campus/detail?jobAdId=uuid-311170879",
        expected_title="软件研发工程师(J12031)",
    )

    assert result.status == "not_applicable"
    assert result.body == ""


def test_positive_route_or_title_conflicts_are_identity_mismatch():
    wrong_route = parse_beisen_legacy_detail(
        CSSC_HTML,
        url=CSSC_URL,
        expected_title="软件研发工程师(J12031)",
        expected_job_id="311170870",
    )
    wrong_title = parse_beisen_legacy_detail(
        CSSC_HTML,
        url=CSSC_URL,
        expected_title="控制设计工程师(J12032)",
        expected_job_id="311170879",
    )

    assert wrong_route.status == "identity_mismatch"
    assert "reason:route_id_mismatch" in wrong_route.identity_evidence
    assert wrong_route.body == ""
    assert wrong_title.status == "identity_mismatch"
    assert "reason:title_mismatch" in wrong_title.identity_evidence


def test_missing_body_is_not_confused_with_identity_conflict():
    result = parse_beisen_legacy_detail(
        "<html><body><h1 class='STJobTitle'>软件研发工程师(J12031)</h1></body></html>",
        url=CSSC_URL,
        expected_title="软件研发工程师(J12031)",
        expected_job_id="311170879",
    )

    assert result.status == "body_missing"
    assert result.identity_status == "matched"
    assert result.status != "identity_mismatch"


def test_missing_title_is_unverified_but_not_a_false_positive_conflict():
    result = parse_beisen_legacy_detail(
        "<main><h2>岗位职责</h2><p>完成测试。</p></main>",
        url=CSSC_URL,
        expected_title="软件研发工程师(J12031)",
        expected_job_id="311170879",
    )

    assert result.status == "identity_unverified"
    assert result.identity_status == "unverified"
    assert "完成测试" in result.body
    assert "reason:title_observation_missing" in result.identity_evidence


def test_module_does_not_import_core_hydration_or_jd_modules():
    source = Path(__file__).parents[1].joinpath(
        "packages", "recruitment_core", "beisen_legacy_detail.py"
    ).read_text(encoding="utf-8")

    assert "import job_details" not in source
    assert "from . import job_details" not in source
    assert "crawlers.jd" not in source


def test_pending_expand_control_is_not_certified_complete():
    result = parse_beisen_legacy_detail(
        """
        <main class='job-detail'>
          <h1>测试岗位</h1>
          <h3>岗位职责：</h3><p>负责算法开发。</p>
          <button>展开更多</button>
        </main>
        """,
        url="https://example.zhiye.com/zpdetail/123",
        expected_title="测试岗位",
        expected_job_id="123",
    )
    assert result.status == "not_ready"
    assert "展开更多" in result.diagnostics["pending_controls"]
