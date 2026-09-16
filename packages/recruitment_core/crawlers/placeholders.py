"""26 家公司的占位爬虫集合。

大部分国内招聘站点是 JS 渲染的 SPA（飞书招聘 / 北森 / Moka / 自建 Vue/React），
requests+BS4 抓不到岗位数据。先用占位实现保证流水线跑通（返回空列表），
后续可参考 unitree.py / huawei.py 的 Playwright 模式逐个升级。

每个子类的 docstring 写明：方向标签、站点平台、升级方向，方便挑选优先升级目标。
"""

import logging

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class PlaceholderCrawler(BaseCrawler):
    """JS 渲染站点的占位爬虫基类。

    子类只需重写 NOTES 类属性说明站点特征。
    升级时把子类的 fetch() 重写为真实抓取（参考 unitree.py / dji.py）。
    """
    NOTES = "JS 渲染 SPA"

    def fetch(self) -> list[dict]:
        logger.warning(
            "[%s] 爬虫为占位实现（%s），当前返回空列表",
            self.company_name, self.NOTES,
        )
        return []


# ── 具身智能 / 人形机器人 ──────────────────────────────────────

class ZhiYuanCrawler(PlaceholderCrawler):
    """智元机器人 — 具身智能头部，GO-1 VLA 模型，估值高速增长。

    平台：自建 Vue SPA。升级方向：Playwright 渲染或抓 /api/* 内部接口。
    """
    NOTES = "自建 Vue SPA"


class GalbotCrawler(PlaceholderCrawler):
    """银河通用 — GraspVLA 抓取大模型，星脑 AstraBrain，估值 200亿+。

    平台：自建 React/Next.js SPA。升级方向：Playwright 渲染。
    """
    NOTES = "自建 React SPA"


class RoboteraCrawler(PlaceholderCrawler):
    """星动纪元 — ERA-42 具身大模型，10亿融资。

    平台：自建 SPA。升级方向：Playwright 渲染。
    """
    NOTES = "自建 SPA"


class FFTAICrawler(PlaceholderCrawler):
    """傅利叶智能 — 人形机器人 GR-1。

    平台：自建网站。升级方向：先 requests 探查静态 HTML，否则 Playwright。
    """
    NOTES = "自建网站"


class UBTechCrawler(PlaceholderCrawler):
    """优必选 — Walker X 人形机器人，香港上市。

    平台：北森招聘 (mokahr/zhiye)。升级方向：抓北森招聘 API，DOM 结构稳定。
    """
    NOTES = "北森招聘系统"


class LimxCrawler(PlaceholderCrawler):
    """逐际动力 — 双足/四足机器人。

    平台：自建 SPA。升级方向：Playwright 渲染。
    """
    NOTES = "自建 SPA"


# ── 机器视觉 / 工业 AI ────────────────────────────────────────

class HikvisionCrawler(PlaceholderCrawler):
    """海康威视 — 安防+机器视觉+移动机器人三栖龙头。

    平台：自建 campushr.hikvision.com。升级方向：Playwright 或抓内部 JSON API。
    """
    NOTES = "自建校招门户"


class DahuaCrawler(PlaceholderCrawler):
    """大华股份 — 安防视觉，旗下华睿科技做工业视觉。

    平台：自建 hr.dahuatech.com。升级方向：Playwright。
    """
    NOTES = "自建校招门户"


class MegviiCrawler(PlaceholderCrawler):
    """旷视科技 — 计算机视觉 + 物流机器人。

    站点实测（2026-05）：实际跳转到 Moka ATS https://app.mokahr.com/campus_apply/megviihr/38642
    点击「职位列表」tab 后页面无渲染内容，DOM 中无岗位卡片。
    Moka 的 group-by-job API 返回 base64 加密响应（含 necromancer 密钥字段），逆向不值得。
    可能 2026 校招未开放或岗位投放在猎聘/Boss 等第三方平台。

    升级方向：观察一段时间，若 Moka 页面后续有内容则 Playwright 点击 tab 后解析；
    或抓取猎聘公司主页 https://www.liepin.com/company/7858396/
    """
    NOTES = "Moka ATS，当前 site 38642 岗位列表空"


class SenseTimeCrawler(PlaceholderCrawler):
    """商汤科技 — 计算机视觉 + 大模型 (日日新)。

    平台：自建/北森。升级方向：Playwright 或北森 API。
    """
    NOTES = "北森/自建"


class OrbbecCrawler(PlaceholderCrawler):
    """奥比中光 — 3D 视觉感知（结构光/ToF）。

    平台：自建网站。升级方向：先 requests 探查，否则 Playwright。
    """
    NOTES = "自建网站"


# ── 自动驾驶 ─────────────────────────────────────────────────

class PonyAICrawler(PlaceholderCrawler):
    """小马智行 — L4 Robotaxi 领头羊。

    平台：自建/Lever ATS。升级方向：抓 Lever 公开 JSON API。
    """
    NOTES = "Lever ATS"


class WeRideCrawler(PlaceholderCrawler):
    """文远知行 — L4 Robotaxi。

    平台：自建/Lever。升级方向：抓 Lever 公开 JSON API。
    """
    NOTES = "Lever ATS"


class MomentaCrawler(PlaceholderCrawler):
    """Momenta — 飞行模式 + 量产辅助驾驶。

    平台：自建 SPA。升级方向：Playwright 渲染。
    """
    NOTES = "自建 SPA"


class HorizonCrawler(PlaceholderCrawler):
    """地平线 — 自动驾驶芯片 + 算法。

    平台：北森招聘 (horizon.zhiye.com)。升级方向：抓北森 API。
    """
    NOTES = "北森招聘系统"


class NioCrawler(PlaceholderCrawler):
    """蔚来 (NIO) — 智能驾驶 + 智能座舱。

    平台：飞书招聘 (nio.jobs.feishu.cn)。升级方向：复用 xiaomi crawler 的飞书模板。
    """
    NOTES = "飞书招聘系统"


# ── 互联网大厂 ────────────────────────────────────────────────

class AlibabaCrawler(PlaceholderCrawler):
    """阿里巴巴 — campus.alibaba.com，AI 岗位占 80%。

    平台：自建 React SPA。升级方向：Playwright 渲染 + 翻页点击。
    """
    NOTES = "自建 React SPA"


class TencentCrawler(PlaceholderCrawler):
    """腾讯 — join.qq.com / careers.tencent.com。

    平台：自建 SPA。升级方向：Playwright 或抓 careers.tencent.com 内部 API。
    """
    NOTES = "自建 SPA"


class MeituanCrawler(PlaceholderCrawler):
    """美团 — campus.meituan.com，AI/自动配送车/无人机方向。

    平台：自建 SPA。升级方向：Playwright 渲染。
    """
    NOTES = "自建 SPA"


class JDCrawler(PlaceholderCrawler):
    """京东 — campus.jd.com，物流机器人/智能仓储相关。

    平台：自建 SPA。升级方向：Playwright 渲染。
    """
    NOTES = "自建 SPA"


class BaiduCrawler(PlaceholderCrawler):
    """百度 — talent.baidu.com，自动驾驶 Apollo + 文心一言。

    平台：自建 SPA。升级方向：Playwright 渲染。
    """
    NOTES = "自建 SPA"


class KuaishouCrawler(PlaceholderCrawler):
    """快手 — campus.kuaishou.cn，多媒体/视觉算法岗。

    平台：自建 SPA。升级方向：Playwright 渲染。
    """
    NOTES = "自建 SPA"


# ── 协作机器人 + 造车 ────────────────────────────────────────

class JakaCrawler(PlaceholderCrawler):
    """节卡机器人 — 协作机械臂 (cobot)，与你 SCARA/机械臂经验直接对口。

    站点实测（2026-05）：jaka.com 是 Nuxt.js SPA，访问 /careers 路径 301 跳回 /zh/home/，
    官网内未找到公开的招聘列表页。可能通过猎聘/Boss/前程无忧投递。

    升级方向：定期检查 jaka.com 是否新增招聘页；或抓 Boss 直聘公司主页。
    """
    NOTES = "官网无公开招聘列表，需走第三方平台"


class DobotCrawler(PlaceholderCrawler):
    """越疆机器人 — Dobot 协作机械臂，与你 SCARA 经验直接对口。

    站点实测（2026-05）：使用北森 ATS 部署在 dobot1.zhiye.com。
    访问 /、/socialPostList、/campus 均返回 Title=Dobot 但 DOM 中无岗位元素，
    岗位数据可能在加密的 /api/Common/* 内部 API 里，未公开 schema。

    升级方向：抓包逆向 zhiye.com 的 GetJobList 类 API；或抓猎聘公司主页。
    """
    NOTES = "北森 ATS dobot1.zhiye.com，DOM 全空"


class XPengCrawler(PlaceholderCrawler):
    """小鹏汽车 — XBOT-L 人形机器人 + 自动驾驶。

    平台：自建 SPA。升级方向：Playwright 渲染。
    """
    NOTES = "自建 SPA"
