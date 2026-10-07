"""User-approved applied baseline and literal terminal status policy."""
import pytest

from packages.domain.application_status_semantics import literal_record_status, explicit_label_status
from packages.tools.application_page_evidence import validate_page_candidate
from tests.test_application_page_model_fallback import case as make_case, candidate, URL
from tests.test_review_whole_card_evidence import reading


def case(tmp_path, monkeypatch, **kwargs):
    kwargs.setdefault('applications', [{'id':'24','title':kwargs['candidates'][0]['card_title'],'record_url':URL}])
    return make_case(tmp_path, monkeypatch, **kwargs)


@pytest.mark.parametrize('line', ['投递简历 2026-09-27', '2026-09-27 18:00 投递', '投递成功',
                                 '投递简历\n2026-09-27', '官网投递 上海 意向城市：上海 投递简历 2026-09-27'])
def test_real_submission_is_applied_without_current_flag(tmp_path, monkeypatch, line):
    title = 'AI应用工程师'
    text = f'{title}\n{line}'
    _, _, _, _, run = case(tmp_path, monkeypatch, observation={'vision': reading(title,text,'',current=False)},
        candidates=[candidate(title, label=line, quote=text, ref='vision:card:0', observed_status='applied',
                              current=False, uncertainties=['只有投递历史，当前阶段不确定'])])
    row = run(visual=True)['24']
    assert row['state'] == 'unchanged', row
    assert not row.get('wrote')


@pytest.mark.parametrize('stage', ['written','interview1','offer','rejected'])
def test_submission_never_rolls_back_later_stage(tmp_path, monkeypatch, stage):
    title, label = 'AI工程师', '投递简历 2026-09-27'
    text = f'{title}\n{label}'
    repo, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{'id':'24','title':title,'record_url':URL,'stage':stage}],
        observation={'vision': reading(title,text,'',current=False)},
        candidates=[candidate(title,label=label,quote=text,ref='vision:card:0',observed_status='applied',current=False)])
    assert not run(visual=True)['24'].get('wrote')
    assert repo.list_applications()[0].stage == stage


@pytest.mark.parametrize('text', [
    'AI工程师\n投递简历', 'AI工程师\n发布日期：2026-09-27\n投递简历',
    'AI工程师\n投递失败 2026-09-27', 'AI工程师\n如果流程结束，进入人才库',
    'AI工程师\n投递简历 2026-09-27\n未通过',
    'AI工程师\n流程终止 2026-09-26\n投递简历 2026-09-27',
    'AI工程师\n流程终止 2026-09-26\n已获得Offer 2026-09-27',
    'AI工程师\n流程终止 2026-09-26\n面试中 2026-09-27',
])
def test_button_negation_or_old_terminal_is_not_positive_evidence(text):
    assert literal_record_status(text) is None


def test_talent_pool_with_ended_process_is_rejected(tmp_path, monkeypatch):
    from tests.test_application_status_model_fallback import _case, _card, FakeClient
    label = '流程已结束，已归入公司人才库。'
    assert explicit_label_status(label) == 'rejected'
    def reject(proposal):
        proposal['candidates'][0]['observed_status'] = 'rejected'
        return proposal
    repo, _, _, run, _ = _case(tmp_path, monkeypatch, cards=[_card('示例工程师',label)], client=FakeClient(reject))
    result = run()
    assert result.updated, result.model_dump()
    assert repo.list_applications()[0].stage == 'rejected'


def test_baidu_explanation_is_not_uncertainty(tmp_path, monkeypatch):
    title, label = 'AI应用工程师', '简历筛选中'
    text = f'{title}\n投递简历\n已完成\n简历筛选\n{label}\n面试\nOffer\n入职'
    _, _, _, _, run = case(tmp_path, monkeypatch, observation={'vision':reading(title,text,label)},
        candidates=[candidate(title,label=label,quote=text,ref='vision:card:0',observed_status='applied',uncertainties=[
            'The card shows a full step timeline (投递简历/简历筛选/面试/Offer/入职); only the 简历筛选 step is marked current, later steps are future and do not upgrade the state.',
            'No explicit rejection or withdrawal evidence exists.'])])
    assert run(visual=True)['24']['state'] == 'unchanged'


def test_recruitment_prefix_keeps_current_evidence(tmp_path, monkeypatch):
    title, label = '测试设备开发工程师', '简历筛选 · 进行中'
    visible = '【27校招-联合动力】' + title
    text = f'{visible}\n{label}\n投递于: 2026-09-23'
    _, _, _, _, run = case(tmp_path,monkeypatch,observation={'vision':reading(visible,text,label)},
        candidates=[candidate(title,label=label,quote=text,ref='vision:card:0',observed_status='applied')])
    assert run(visual=True)['24']['state'] == 'unchanged'


def test_volunteer_badge_does_not_veto_literal_terminal(tmp_path, monkeypatch):
    title = 'AI Agent开发工程师'
    text = f'{title} 第2志愿 官网投递\n投递简历 2026-09-27\n流程终止 2026-09-28'
    repo, _, _, _, run = case(tmp_path,monkeypatch,observation={'vision':reading(title,text,'第2志愿')},
        candidates=[candidate(title,label='流程终止',quote=text,ref='vision:card:0',observed_status='rejected')])
    assert run(visual=True)['24']['state'] == 'updated'
    assert repo.list_applications()[0].stage == 'rejected'


def test_foreign_card_terminal_cannot_be_borrowed():
    text = '开发工程师\n投递简历 2026-09-27\n测试工程师\n流程终止 2026-09-28'
    app = {'id':'one','job_title':'开发工程师'}
    card, error = validate_page_candidate({'vision':reading('开发工程师',text,'流程终止')},app,
        [app,{'id':'two','job_title':'测试工程师'}],card_title='开发工程师',quotation=text,
        source_ref='vision:card:0',label='流程终止',status='rejected',current=True,visual=True)
    assert card is None
    assert error == 'target_record_ambiguous'


@pytest.mark.parametrize('stage', ['applied','written','interview1'])
def test_dom_submission_resolves_without_a_model_and_never_regresses(tmp_path,monkeypatch,stage):
    from packages.tools import application_status_model as model
    title, label = 'AI应用工程师', '投递简历\n2026-09-27'
    text = f'{title}\n{label}'
    repo, _, _, client, run = case(tmp_path,monkeypatch,
        applications=[{'id':'24','title':title,'record_url':URL,'stage':stage}],
        observation={'application_records':[{'title':title,'context':text,'status':'','label':''}]},
        candidates=[candidate(title)])
    monkeypatch.setattr(model,'configured_model_client',lambda _: pytest.fail('No model needed for a literal submitted record'))
    row=run()['24']
    assert row['state']=='unchanged',row
    assert not row.get('wrote') and not client.calls
    assert repo.list_applications()[0].stage==stage


def test_model_cannot_truncate_terminal_outcome_to_submission(tmp_path,monkeypatch):
    title='AI工程师'
    submitted=f'{title}\n投递简历 2026-09-27'
    text=submitted+'\n流程终止 2026-09-28'
    _, _, _, _, run = case(tmp_path,monkeypatch,observation={'vision':reading(title,text,'流程终止')},
        candidates=[candidate(title,label='投递简历',quote=submitted,ref='vision:card:0',observed_status='applied',current=False)])
    assert run(visual=True)['24']['state']=='unresolved'
