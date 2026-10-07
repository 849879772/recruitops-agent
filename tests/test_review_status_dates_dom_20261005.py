"""Synthetic DOM only; keep Playwright's loop separate from model-flow tests."""

import subprocess

import pytest

from packages.domain.application_status_semantics import status_evidence_conflict
from test_desktop_browser import CONTEXT, browser, node_call, observe, page


def test_meituan_style_submission_date_does_not_conflict_with_current_written(page):
    _, result = observe(page, """<div class='application-card'>
      <p>志愿一</p><h3>AI Agent开发工程师</h3>
      <p>投递时间：2026/09/30 21:58:46</p><p>笔试</p></div>""")
    record = result["result"]["application_records"][0]
    assert (record["status"], record["label"]) == ("written", "笔试")
    assert record["raw_status_labels"] == ["笔试"]
    assert record["signals"]["has_date"]
    assert record["applied_at"].startswith("2026/09/30")
    assert "投递时间：2026/09/30" in record["context"]
    assert not status_evidence_conflict(record)


@pytest.mark.parametrize("prefix", ["申请时间", "投递时间"])
def test_dom_inactive_ladder_uses_dated_submission_only_as_baseline(page, prefix):
    _, result = observe(page, f"""<article data-recruitops-application>
      <h3>应用软件开发工程师</h3><p>{prefix}：2026-10-01</p>
      <ol class='steps'><li>笔试</li><li>面试</li><li>offer</li></ol>
      <p>未开始</p><button>点击这里开始</button></article>""")
    record = result["result"]["application_records"][0]
    assert record["status"] == "applied"
    assert record["label"] == f"{prefix}：2026-10-01"
    assert record["evidence_source"] == "submission-date"
    assert not record["signals"]["has_active_step"]
    assert not status_evidence_conflict(record)


@pytest.mark.parametrize("date", ["2026-02-30", "2026-10-01 24:00"])
def test_dom_invalid_submission_date_does_not_create_baseline(page, date):
    _, result = observe(page, f"""<article data-recruitops-application>
      <h3>应用软件开发工程师</h3><p>申请时间：{date}</p>
      <ol><li>笔试</li><li>面试</li><li>offer</li></ol></article>""")
    record = result["result"]["application_records"][0]
    assert record["status"] == "" and record["evidence_source"] != "submission-date"


def test_true_two_current_steps_remain_conflicting_even_with_a_submission_date(page):
    _, result = observe(page, """<article data-recruitops-application>
      <h3>AI Agent开发工程师</h3><p>投递时间：2026-09-30</p>
      <p>当前状态：面试</p><p aria-current='step'>笔试</p></article>""")
    record = result["result"]["application_records"][0]
    assert record["status"] == ""
    assert record["raw_status_labels"] == ["面试", "笔试"]
    assert status_evidence_conflict(record)


def context_card(tail):
    return ("<article data-recruitops-application><span data-job-title>软件工程师</span>"
            "<br>当前状态：已投递<br>" + tail + "</article>")


@pytest.mark.parametrize("length,truncated", [(120, False), (1000, False), (1001, True), (2600, True)])
def test_actual_card_context_truncation_survives_adapter_without_boundary_false_positive(page, length, truncated):
    prefix = "软件工程师 当前状态：已投递 "
    expected = prefix + "注" * (length - len(prefix))
    _, result = observe(page, context_card("注" * (length - len(prefix))))
    record = result["result"]["application_records"][0]
    assert record["context"] == expected[:1000]
    assert record["evidence"] == record["context"]
    assert record["signals"]["context_truncated"] is truncated
    assert (record["status"], record["label"]) == ("applied", "已投递")


def test_upstream_parser_cap_is_reported_even_when_redaction_shortens_context(page):
    synthetic_email = "a" * 1800 + "@fixture.invalid"
    _, result = observe(page, context_card(synthetic_email + " " + "注" * 400))
    record = result["result"]["application_records"][0]
    assert len(record["context"]) < 1000
    assert "[redacted-email]" in record["context"]
    assert record["signals"]["context_truncated"] is True


@pytest.mark.parametrize("key,value", [("untrusted_extra_signal", True), ("context_truncated", "true")])
def test_context_truncation_flag_does_not_open_adapter_signal_schema(page, key, value):
    raw, _ = observe(page, context_card("短备注"))
    raw["data"]["applicationRecords"][0]["signals"][key] = value
    with pytest.raises(subprocess.CalledProcessError):
        node_call("a.normalizeObservation(input.raw,input.context)", {"raw": raw, "context": CONTEXT})
