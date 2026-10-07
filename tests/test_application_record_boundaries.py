"""Synthetic DOM only: these fixtures are not copies of employer websites."""

import subprocess

import pytest

from test_desktop_browser import CONTEXT, browser, node_call, observe, page


def test_recommendation_subcard_cannot_replace_actual_job_title(page):
    _, result = observe(page, """<main><div class='application-record' data-application-id='fixture-a'>
      <div class='job-name'>秋招AI平台研发工程师-武汉</div>
      <div class='record-status-card'><h3 class='title'>推荐到其他职位</h3>
        <p>当前状态：推荐到其他职位</p><time>2026-09-14 21:43</time>
      </div></div>
      <div class='application-record' data-application-id='fixture-b'>
        <div class='job-name'>秋招AI研发工程师（Python/Java/Go）-武汉</div>
        <p>状态：筛选阶段</p><time>2026-09-17 14:42</time>
      </div></main>""")
    records = result['result']['application_records']
    assert len(records) == 2
    assert records[0]['title'] == '秋招AI平台研发工程师-武汉'
    assert records[0]['application_id'] == 'fixture-a'
    assert records[0]['label'] == '推荐到其他职位'
    assert records[0]['status'] == ''
    assert records[1]['title'] == '秋招AI研发工程师（Python/Java/Go）-武汉'
    assert records[1]['application_id'] == 'fixture-b'


@pytest.mark.parametrize('ladder', [
    '<ol class="steps"><li>申请成功</li><li>简历筛选</li><li>笔试</li><li>面试</li><li>offer</li></ol>',
    '<div class="status-progress">申请成功 → 简历筛选 → 笔试 → 面试 → offer</div>',
    '<p>当前进度</p><p>申请成功</p><p>简历筛选</p><p>笔试</p><p>面试</p><p>offer</p>',
    '<p>当前进度：1 申请成功 2 简历筛选 3 笔试 4 面试 5 offer</p>',
    '<ol class="steps active"><li>申请成功</li><li>笔试</li><li>面试</li><li>offer</li></ol>',
])
def test_progress_ladder_without_current_is_not_a_status(page, ladder):
    _, result = observe(page, f"<article data-recruitops-application><h3>AI应用岗</h3><time>2026-09-20</time>{ladder}</article>")
    record = result['result']['application_records'][0]
    assert record['status'] == '' and record['current_step_label'] == ''
    assert record['evidence_source'] == 'timeline-without-current'
    assert record['signals']['has_progress_timeline'] is True
    assert record['signals']['current_step_identified'] is False
    assert 'offer' in record['stage_labels']
    assert result['result']['entries'] == []


@pytest.mark.parametrize('current', ['<p>当前进度：笔试</p>', ''])
def test_current_step_stays_separate_from_all_steps(page, current):
    selected = '' if current else " aria-current='step'"
    _, result = observe(page, f"""<article data-recruitops-application><h3>AI应用岗</h3>
      <time>2026-09-20</time>{current}<ol class='steps'><li>申请成功</li>
      <li{selected}>笔试</li><li>面试</li><li>offer</li></ol></article>""")
    record = result['result']['application_records'][0]
    assert record['status'] == 'written' and record['current_step_label'] == '笔试'
    assert record['signals']['current_step_identified'] is True
    assert record['signals']['has_progress_timeline'] is True
    assert record['raw_status_labels'] == ['笔试']
    assert result['result']['entries'][0]['status'] == 'written'


def test_conflicting_current_markers_do_not_choose_last_step(page):
    _, result = observe(page, """<article data-recruitops-application><h3>软件工程师</h3>
      <ol class='steps'><li aria-current='step'>笔试</li><li class='active'>面试</li><li>offer</li></ol></article>""")
    record = result['result']['application_records'][0]
    assert record['status'] == '' and record['current_step_label'] == ''
    assert record['signals']['conflicting_statuses'] is True
    assert record['signals']['current_step_identified'] is False


def test_complete_unclassified_rows_and_labeled_rows_are_bounded(page):
    _, result = observe(page, """<main>
      <div><span>软件工程师</span><span>2026-09-20</span><span>面试</span></div>
      <ul><li><div>AI应用岗</div><div>2026-09-21</div><div>笔试</div></li></ul>
      <div><div>岗位：质量专员</div><div>当前状态：简历筛选</div></div>
      <table><tbody><tr><td>测试开发工程师</td><td>2026-09-22</td><td>申请成功</td></tr></tbody></table>
      </main>""")
    records = result['result']['application_records']
    assert {row['title']: row['status'] for row in records} == {
        '软件工程师': 'interview', 'AI应用岗': 'written', '质量专员': 'applied', '测试开发工程师': 'applied'}
    assert len(records) == 4
    assert all(row['signals']['current_step_identified'] for row in records)


def test_readable_row_keeps_conflicting_explicit_status_nodes(page):
    _, result = observe(page, """<div><span>软件工程师</span><time>2026-09-20</time>
      <p>当前状态：笔试</p><span data-recruitops-application-status>面试</span></div>""")
    record = result['result']['application_records'][0]
    assert record['status'] == '' and record['current_step_label'] == ''
    assert record['signals']['conflicting_statuses'] is True
    assert result['result']['entries'] == []


def test_unrelated_rows_and_navigation_cannot_supply_missing_record_parts(page):
    _, result = observe(page, """<nav><div><span>软件工程师</span><span>2026-09-20</span><span>面试</span></div></nav>
      <main><div><span>软件工程师</span></div><div><time>2026-09-20</time></div>
      <div><span>面试</span></div><div>只有说明文字</div></main>
      <div><nav><span>测试工程师</span></nav><p>2026-09-20</p><p>当前状态：面试</p></div>""")
    assert result['result']['application_records'] == []


def test_readable_row_without_date_or_status_label_remains_unparsed(page):
    _, result = observe(page, '<main><div><span>软件工程师</span><span>面试</span></div></main>')
    assert result['result']['application_records'] == []


def test_title_job_code_is_preserved_as_external_identity(page):
    _, result = observe(page, """<article data-recruitops-application><h3>AI应用开发工程师（AI-Coding方向）(J22541)</h3>
      <p>当前状态：筛选阶段</p><time>2026-09-20</time></article>""")
    record = result['result']['application_records'][0]
    assert record['job_id'] == 'J22541'
    assert record['raw_title'] == 'AI应用开发工程师（AI-Coding方向）(J22541)'
    assert 'job_id' not in result['result']['entries'][0]


def test_adapter_rejects_unbounded_or_mistyped_progress_fields(page):
    raw, _ = observe(page, '<article data-recruitops-application><h3>软件工程师</h3><p>申请成功</p></article>')
    record = raw['data']['applicationRecords'][0]
    record['stage_labels'] = ['面试'] * 31
    with pytest.raises(subprocess.CalledProcessError):
        node_call('a.normalizeObservation(input.raw,input.context)', {'raw': raw, 'context': CONTEXT})
    record['stage_labels'] = []
    record['signals']['current_step_identified'] = 'yes'
    with pytest.raises(subprocess.CalledProcessError):
        node_call('a.normalizeObservation(input.raw,input.context)', {'raw': raw, 'context': CONTEXT})


@pytest.mark.parametrize('date', ['0:26', '今天 00:26', '昨天 23:58', '3分钟前'])
def test_nested_plain_cards_accept_displayed_relative_or_time_only_dates(page, date):
    _, result = observe(page, f"""<main><h2>投递记录</h2>
      <div><div><span>2027届校招-质量开发工程师（示例城）</span><a href='/job?jobId=fixture-quality'>查看详情</a></div>
        <div><span>状态</span><span> : </span><span>申请成功</span></div>
        <div>项目：-</div><time>{date}</time></div>
      <div><div><span>2027届校招-系统工程师（示例城）</span><a href='/job?jobId=fixture-system'>查看详情</a></div>
        <div><span>状态</span><span> : </span><span>申请成功</span></div>
        <div>项目：-</div><time>0:25</time></div></main>""")
    records = result['result']['application_records']
    assert [(row['title'], row['status'], row['applied_at']) for row in records] == [
        ('2027届校招-质量开发工程师（示例城）', 'applied', date),
        ('2027届校招-系统工程师（示例城）', 'applied', '0:25'),
    ]
    assert [row['job_id'] for row in records] == ['fixture-quality', 'fixture-system']
    assert all(row['raw_status_labels'] == ['申请成功'] for row in records)
    assert all(row['signals']['current_step_identified'] for row in records)


def test_undated_preference_cards_keep_timeline_without_guessing_current_status(page):
    cards = ''.join(f"""<div><div><span>{title}</span></div><div>应届生</div><div>软件类</div>
      <div>意向岗位：第{order}意向</div><div>意向城市：示例甲市、示例乙市</div>
      <ol class='steps'><li><span>简历投递</span><div>成功</div></li>
        <li><span>筛选</span><div>待评估</div><button>撤销申请</button></li>
        <li>面试</li><li>Offer</li><li>入职</li></ol></div>"""
      for title, order in [('智能系统工程师', '一'), ('质量开发工程师', '二')])
    _, result = observe(page, f'<main><h2>应聘记录</h2><p>共2条</p>{cards}</main>')
    records = result['result']['application_records']
    assert [row['title'] for row in records] == ['智能系统工程师', '质量开发工程师']
    assert [row['volunteer_index'] for row in records] == ['一', '二']
    assert all(row['status'] == '' and row['current_step_label'] == '' for row in records)
    assert all(row['evidence_source'] == 'timeline-without-current' for row in records)
    assert all(row['signals']['has_operation'] and row['signals']['has_progress_timeline'] for row in records)
    assert all(row['applied_at'] == '' and 'Offer' in row['stage_labels'] for row in records)
    assert result['result']['entries'] == []


def test_concatenated_numbered_ladder_keeps_card_without_current_marker(page):
    _, result = observe(page, """<main><div><div>平台开发工程师</div>
      <button>修改申请</button><button>撤回</button><div>项目:-2026-08-16 3:32</div>
      <div>1投递成功2综合测评3岗位笔试4专业面试5综合面试6终试洽谈7签约</div>
      </div></main>""")
    records = result['result']['application_records']
    assert len(records) == 1
    record = records[0]
    assert record['title'] == '平台开发工程师'
    assert record['status'] == '' and record['current_step_label'] == ''
    assert record['evidence_source'] == 'timeline-without-current'
    assert record['stage_labels'] == ['投递成功', '综合测评', '岗位笔试', '专业面试', '综合面试', '终试洽谈', '签约']
    assert result['result']['entries'] == []


@pytest.mark.parametrize('marker', ["class='is-process'", "class='ant-steps-item-process'", "aria-current='step'"])
@pytest.mark.parametrize('label,status', [('岗位笔试', 'written'), ('初试', 'interview'), ('复试', 'interview')])
def test_only_explicit_current_markers_map_extended_step_labels(page, marker, label, status):
    _, result = observe(page, f"""<article data-recruitops-application><h3>平台开发工程师</h3>
      <ol class='steps'><li>投递成功</li><li {marker}>{label}</li><li>签约</li></ol></article>""")
    record = result['result']['application_records'][0]
    assert record['status'] == status and record['current_step_label'] == label
    assert record['raw_status_labels'] == [label]
    assert record['signals']['has_active_step'] is True


def test_time_and_operation_alone_are_not_current_stage_evidence(page):
    _, result = observe(page, """<main><div class='record'><span>平台开发工程师</span>
      <button>修改申请</button><time>今天 08:30</time></div></main>""")
    record = result['result']['application_records'][0]
    assert record['title'] == '平台开发工程师'
    assert record['applied_at'] == '今天 08:30'
    assert record['status'] == '' and record['raw_status_labels'] == []
    assert record['evidence_source'] == 'record-exists-only'
    assert record['signals']['current_step_identified'] is False
    assert result['result']['entries'] == []
