from .beisen import BeisenRecruitCrawler
from .beisen_mobile import BeisenMobileCrawler
from .bytedance import ByteDanceCrawler
from .tencent import TencentCrawler
from .tencent_music import TencentMusicCampusCrawler
from .qiyunfang import QiyunfangCrawler
from .ourats import OurATSCrawler
from .lixiang import LixiangCrawler
from .aifly import AiflyCrawler
from .gtasemi import GtaSemiCrawler
from .vayoinfo import VayoInfoCrawler
from .elex import ElexCampusCrawler
from .chaitin import ChaitinCrawler
from .tauren import TaurenCrawler
from .iqitek import IqiTekCrawler
from .jiexun import JiexunCrawler
from .isoftstone import IsoftstoneCrawler
from .ourpalm import OurPalmCrawler
from .meituan import MeituanCrawler
from .baidu import BaiduCrawler
from .kuaishou import KuaishouCrawler
from .generic_render import GenericRenderCrawler
from .tonghuashun import TonghuashunCampusCrawler
from .greenvalley import GreenvalleyCrawler
from .static_html import StaticHtmlCrawler
from .bilibili import BilibiliCrawler
from .feishu import GenericFeishuCrawler
from .alibaba import AlibabaCrawler
from .jd import JDCrawler
from .mihoyo import MihoyoCrawler
from .gbits import GbitsCrawler
from .oppo import OppoCrawler
from .sf import SFCrawler
from .byd import BYDCrawler
from .netease import NetEaseCrawler
from .leihuo import LeihuoCrawler
from .boe import BOECrawler
from .cvte import CVTECrawler
from .lenovo import LenovoCrawler
from .dji import DJICrawler
from .huawei import HuaweiCrawler
from .job4399 import Job4399Crawler
from .tplink import TPLinkCrawler
from .moka import MokaRecruitCrawler
from .moseeker import MoSeekerCrawler
from .hotjob import HotjobRecruitCrawler
from .hikvision import HikvisionCrawler
from .unitree import UnitreeCrawler
from .xiaomi import XiaomiCrawler
from .inovance import InovanceRecruitCrawler
from .huayan import HuayanCrawler
from .pdd import PDDCrawler
from .iguopin import IGuopinCrawler
from .itek import ItekCrawler
from .xtimes import XTimesCrawler
from .zmotion import ZmotionCrawler
from .fanruan import FanruanCrawler
from .dekeinfo import DekeInfoCrawler
from .duoyi import DuoyiCrawler
from .placeholders import (
    # 具身智能 / 人形机器人
    ZhiYuanCrawler, GalbotCrawler, RoboteraCrawler, FFTAICrawler, UBTechCrawler, LimxCrawler,
    # 机器视觉 / 工业 AI（海康已迁出到 crawlers/hikvision.py 真抓）
    DahuaCrawler, MegviiCrawler, SenseTimeCrawler, OrbbecCrawler,
    # 自动驾驶
    PonyAICrawler, WeRideCrawler, MomentaCrawler, HorizonCrawler, NioCrawler,
    # 互联网大厂（字节、腾讯、美团、阿里、京东、快手、百度已用真实 crawler，从此处排除）
    # 协作机器人 + 造车
    JakaCrawler, DobotCrawler, XPengCrawler,
)

CRAWLER_MAP = {
    # 已实现（Playwright 真抓）
    "dji": DJICrawler,
    "huawei": HuaweiCrawler,
    "job4399": Job4399Crawler,
    "tplink": TPLinkCrawler,
    "xiaomi": XiaomiCrawler,
    "unitree": UnitreeCrawler,
    "inovance": InovanceRecruitCrawler,
    "huayan": HuayanCrawler,

    # 平台级爬虫（一个类服务多家，按 careers_url 解析 slug/子域名/host）
    "moka": MokaRecruitCrawler,
    "moseeker": MoSeekerCrawler,
    "beisen": BeisenRecruitCrawler,
    "beisen_mobile": BeisenMobileCrawler,
    "feishu": GenericFeishuCrawler,
    "hotjob": HotjobRecruitCrawler,

    # 具身智能 / 人形机器人
    "zhiyuan": ZhiYuanCrawler,
    "galbot": GalbotCrawler,
    "robotera": RoboteraCrawler,
    "fftai": FFTAICrawler,
    "ubtech": UBTechCrawler,
    "limx": LimxCrawler,

    # 机器视觉 / 工业 AI
    "hikvision": HikvisionCrawler,
    "dahua": DahuaCrawler,
    "megvii": MegviiCrawler,
    "sensetime": SenseTimeCrawler,
    "orbbec": OrbbecCrawler,

    # 自动驾驶
    "ponyai": PonyAICrawler,
    "weride": WeRideCrawler,
    "momenta": MomentaCrawler,
    "horizon": HorizonCrawler,
    "nio": NioCrawler,

    # 互联网大厂
    "alibaba": AlibabaCrawler,
    "tencent": TencentCrawler,
    "tencent_music": TencentMusicCampusCrawler,
    "qiyunfang": QiyunfangCrawler,
    "ourats": OurATSCrawler,
    "lixiang": LixiangCrawler,
    "aifly": AiflyCrawler,
    "gtasemi": GtaSemiCrawler,
    "vayoinfo": VayoInfoCrawler,
    "elex": ElexCampusCrawler,
    "chaitin": ChaitinCrawler,
    "tauren": TaurenCrawler,
    "iqitek": IqiTekCrawler,
    "jiexun": JiexunCrawler,
    "isoftstone": IsoftstoneCrawler,
    "ourpalm": OurPalmCrawler,
    "bytedance": ByteDanceCrawler,
    "meituan": MeituanCrawler,
    "jd": JDCrawler,
    "mihoyo": MihoyoCrawler,
    "gbits": GbitsCrawler,
    "oppo": OppoCrawler,
    "sf": SFCrawler,
    "byd": BYDCrawler,
    "netease": NetEaseCrawler,
    "leihuo": LeihuoCrawler,
    "boe": BOECrawler,
    "cvte": CVTECrawler,
    "lenovo": LenovoCrawler,
    "baidu": BaiduCrawler,
    "kuaishou": KuaishouCrawler,
    "bilibili": BilibiliCrawler,
    "pdd": PDDCrawler,
    "iguopin": IGuopinCrawler,
    "itek": ItekCrawler,
    "xtimes": XTimesCrawler,
    "zmotion": ZmotionCrawler,
    "fanruan": FanruanCrawler,
    "dekeinfo": DekeInfoCrawler,
    "duoyi": DuoyiCrawler,
    "render": GenericRenderCrawler,   # 通用渲染爬虫(自动选主选择器)：长尾自建 SPA 站
    "tonghuashun": TonghuashunCampusCrawler,
    "greenvalley": GreenvalleyCrawler,
    "static_html": StaticHtmlCrawler,  # 通用静态官网职位列表页

    # 协作机器人 + 造车
    "jaka": JakaCrawler,
    "dobot": DobotCrawler,
    "xpeng": XPengCrawler,
}
