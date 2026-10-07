"""Offline DOM fixtures; no employer data, network, profile or database."""

from packages.domain.application_status_semantics import literal_record_status
from test_desktop_browser import browser, observe, page


TITLE = "AI测试开发工程师"


def test_personal_dated_ladder_and_end_action_are_applied_not_rejected(page):
    _, result = observe(page, f"""<article class='application-card current'>
      <h3>{TITLE}</h3><button>变更职位</button><button>催促流程</button>
      <button>结束流程</button><p>投递时间：2026-09-27</p>
      <ol class='steps'><li>投递简历</li><li>简历筛选</li><li>面试</li>
      <li>录用评估</li><li>offer</li><li>预入职</li></ol><p>流程中</p></article>""")
    record = result["result"]["application_records"][0]
    assert record["status"] == "applied", record
    assert record["label"] == "投递时间：2026-09-27"
    assert record["evidence_source"] == "submission-date"
    assert record["signals"]["conflicting_statuses"] is False
    assert record["signals"]["has_active_step"] is False
    assert literal_record_status(record["context"]) == ("applied", record["label"])


def test_preference_components_do_not_replace_outer_job_title(page):
    _, result = observe(page, """<main><article class='application-card'>
      <h2>【2027校园招聘】AI Agent开发工程师</h2>
      <p>第一志愿已激活</p><p>杭州市</p>
      <section class='preference-card'><h3 class='title'>第一意向</h3>
      <p>软件产品</p><p>最新状态：简历评估</p>
      <ol class='steps'><li class='active'>简历投递</li><li>简历评估</li>
      <li>面试</li><li>offer</li><li>入职</li></ol></section></article>
      <article class='application-card'><h2>【2027校园招聘】应用软件开发工程师</h2>
      <p>第二志愿</p><section class='preference-card'><h3 class='title'>第一意向</h3>
      <p>最新状态：等待处理</p></section></article></main>""")
    records = result["result"]["application_records"]
    assert [record["raw_title"] for record in records] == [
        "【2027校园招聘】AI Agent开发工程师", "【2027校园招聘】应用软件开发工程师"]
    assert [record["status"] for record in records] == ["applied", "applied"]
    assert [record["label"] for record in records] == ["简历评估", "等待处理"]
    assert all(not record["signals"]["conflicting_statuses"] for record in records)


def test_an_active_whole_card_is_not_an_active_process_step(page):
    _, result = observe(page, f"""<article data-recruitops-application class='active'>
      <h3>{TITLE}</h3><ol><li>投递简历</li><li>简历筛选</li>
      <li>面试</li><li>offer</li></ol><button>结束流程</button></article>""")
    record = result["result"]["application_records"][0]
    assert record["status"] == "" and not record["signals"]["has_active_step"]
    assert not record["signals"]["conflicting_statuses"]
