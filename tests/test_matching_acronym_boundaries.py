import pytest

from packages.matching.models import Direction
from packages.matching.rules import classify_job_directions


@pytest.mark.parametrize('title,body', [
    ('Android开发工程师', '熟悉Application、Fragment、Intent知识及Java语言。'),
    ('网络运维工程师', '熟悉VLAN网络与MQTT协议。'),
    ('系统开发工程师', '使用Microsoft Office进行文档编辑。'),
])
def test_english_word_substrings_are_not_target_acronyms(title, body):
    result = classify_job_directions({'title': title, 'jd_raw': body})
    assert not result.matched_directions


@pytest.mark.parametrize('title,body,direction', [
    ('Agent研发工程师', '负责RAG检索和Agent工具调用。', Direction.LLM_AGENT),
    ('机器人开发工程师', '熟悉ROS2软件开发。', Direction.ROBOT_ARM),
    ('算法研究员', '研究VLA模型微调。', Direction.EMBODIED_LEARNING),
    ('客户端开发工程师', '熟悉Qt5图形界面。', Direction.CPP_SOFTWARE),
    ('开发工程师', '使用RAGFlow搭建检索系统。', Direction.LLM_AGENT),
])
def test_real_acronyms_adjacent_to_chinese_are_preserved(title, body, direction):
    assert direction in classify_job_directions({'title': title, 'jd_raw': body}).matched_directions
