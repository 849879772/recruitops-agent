from .feishu import FeishuRecruitCrawler


class XiaomiCrawler(FeishuRecruitCrawler):
    """小米校招（飞书招聘 /campus 校招频道）。

    DOM 与翻页逻辑见 FeishuRecruitCrawler，这里只配站点参数。
    """

    LIST_URL = "https://xiaomi.jobs.f.mioffice.cn/campus"
    HOST = "https://xiaomi.jobs.f.mioffice.cn"
    MAX_PAGES = 200
    GOTO_WAIT_UNTIL = "networkidle"
    GOTO_TIMEOUT_MS = 60000
    JD_RAW_LIMIT = 12000
