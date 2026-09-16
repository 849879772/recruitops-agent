from __future__ import annotations

import html
import json

from packages.recruitment_core.crawlers.moka import MokaRecruitCrawler


def _anchors(prefix: str, count: int) -> str:
    return "".join(
        f'<a href="#/job/{prefix}-{index}"><span>Engineer {index}</span></a>'
        for index in range(count)
    )


def test_dom_total_stays_in_the_job_scope_and_ignores_delivery_limit() -> None:
    page = f"""
    <div class="promo-banner">热招 99 个职位</div>
    <section class="jobs-campus">
      <div class="delivery-limit">允许3个月内投递3个职位</div>
      <div class="job-result-count">12结果</div>
      {_anchors("campus", 12)}
    </section>
    <div class="annual-banner">2027届校园招聘，88个岗位</div>
    """

    assert MokaRecruitCrawler._result_count(page) == 12


def test_visibility_hidden_scope_before_visible_campus_scope_is_ignored() -> None:
    page = f"""
    <section class="jobs-social" style="visibility:hidden">
      <div class="job-result-count">3结果</div>
      {_anchors("social", 3)}
    </section>
    <section class="jobs-campus">
      <div class="job-result-count">12结果</div>
      {_anchors("campus", 12)}
    </section>
    """

    assert MokaRecruitCrawler._result_count(page) == 12


def test_advertisement_and_year_numbers_without_a_list_count_are_ignored() -> None:
    page = f"""
    <div class="advertisement">广告位：100个岗位</div>
    <section class="jobs-campus">
      <div class="campaign-banner">2027年度招聘</div>
      {_anchors("campus", 2)}
    </section>
    """

    assert MokaRecruitCrawler._result_count(page) is None


def test_detail_text_is_not_used_as_a_listing_total() -> None:
    page = f"""
    <section class="jobs-campus">
      <div class="job-detail">岗位（2）：负责详情与任职要求</div>
      {_anchors("campus", 12)}
    </section>
    """

    assert MokaRecruitCrawler._result_count(page) is None


def test_init_data_total_is_selected_from_the_matching_listing_scope() -> None:
    init_data = {
        "campus": {
            "jobStats": {"total": 12},
            "jobs": [{"id": f"campus-{index}"} for index in range(12)],
        },
        "social": {
            "jobStats": {"total": 3},
            "jobs": [{"id": f"social-{index}"} for index in range(3)],
        },
    }
    encoded = html.escape(json.dumps(init_data), quote=True)
    page = f"""
    <input id="init-data" value="{encoded}">
    <section class="jobs-campus" data-active="true">{_anchors("campus", 12)}</section>
    <section class="jobs-social">{_anchors("social", 3)}</section>
    """

    assert MokaRecruitCrawler._result_count(page) == 12


def test_multiple_visible_scopes_without_current_binding_are_unknown() -> None:
    page = f"""
    <section class="jobs-campus">
      <div class="job-result-count">12结果</div>
      {_anchors("campus", 12)}
    </section>
    <section class="jobs-social">
      <div class="job-result-count">3结果</div>
      {_anchors("social", 3)}
    </section>
    """

    assert MokaRecruitCrawler._result_count(page) is None


def test_short_init_total_cannot_override_the_current_list_dom_count() -> None:
    init_data = html.escape(
        json.dumps({"jobStats": {"total": 3}}),
        quote=True,
    )
    page = f"""
    <input id="init-data" value="{init_data}">
    <section class="jobs-campus">
      <div class="job-result-count">12结果</div>
      {_anchors("campus", 12)}
    </section>
    """

    assert MokaRecruitCrawler._result_count(page) == 12


def test_explicit_scoped_short_total_remains_a_source_conflict() -> None:
    page = f"""
    <section class="jobs-campus">
      <div class="job-result-count">2个职位</div>
      {_anchors("campus", 30)}
    </section>
    """

    assert MokaRecruitCrawler._result_count(page) == 2


def test_listing_numbers_without_a_scoped_total_do_not_become_advertised_total() -> None:
    page = f"""
    <div>允许3个月内投递3个职位，2027届招聘</div>
    <section class="jobs-campus">{_anchors("campus", 12)}</section>
    """

    assert MokaRecruitCrawler._result_count(page) is None
