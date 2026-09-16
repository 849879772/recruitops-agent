from __future__ import annotations

import pytest

from packages.matching.jd_quality import assess_jd_quality


JD = (
    "\u3010\u5c97\u4f4d\u804c\u8d23\u3011\n"
    "\u8d1f\u8d23 C++ \u63a7\u5236\u7cfb\u7edf\u5f00\u53d1\u3002\n"
    "\u3010\u4efb\u804c\u8981\u6c42\u3011\n"
    "\u719f\u6089 Linux\uff0c\u5177\u5907 Qt \u5f00\u53d1\u7ecf\u9a8c\u3002\n"
)
QUOTA = (
    "2027\u5c4a\u5141\u8bb8\u6295\u90123\u6b21,"
    "\u8bf7\u9009\u62e9\u9002\u5408\u7684\u804c\u4f4d\u8fdb\u884c\u6295\u9012 "
    "\u7acb\u5373\u6295\u9012"
)


@pytest.mark.parametrize(
    "label",
    [
        "\u5e94\u5c4a\u751f\u6749\u5c16\u8ba1\u5212-",
        "\u672a\u6765\u4eba\u624d\u9879\u76ee-",
        "\u542f\u822a\u4e13\u9879:",
        "2027\u5c4a\u6821\u62db_",
    ],
)
def test_repeated_jd_with_campaign_label_and_quota_is_complete(label: str) -> None:
    assert assess_jd_quality(JD + JD + label + QUOTA).complete


def test_campaign_footer_does_not_hide_different_job_content() -> None:
    other_jd = JD.replace("C++", "Python").replace("Qt", "SQL")
    quality = assess_jd_quality(JD + other_jd + "\u542f\u822a\u4e13\u9879:" + QUOTA)
    assert not quality.complete
    assert quality.reason_code == "cross_job_content"


def test_quota_does_not_strip_arbitrary_extra_requirements() -> None:
    extra = "\u5177\u59073\u5e74\u4e13\u7528\u63a7\u5236\u5668\u5f00\u53d1\u7ecf\u9a8c\u3002"
    quality = assess_jd_quality(JD + JD + extra + QUOTA)
    assert not quality.complete
    assert quality.reason_code == "cross_job_content"
