"""Shared URL-only recruitment entry diagnosis and adapter routing."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from packages.discovery.oc_capture import classify_oc_destination_url


# Host→crawler mapping from companies.yaml (197 hosts with dedicated crawlers)
_COMPANIES_YAML_HOST_MAP = {
    # alibaba (6)
    "campus-talent.alibaba.com": "alibaba",
    "campus.alibaba.com": "alibaba",
    "career.fliggy.com": "alibaba",
    "talent.amap.com": "alibaba",
    "talent.lingxigames.com": "alibaba",
    "talent.taotian.com": "alibaba",
    # baidu (1)
    "talent.baidu.com": "baidu",
    # beisen (96)
    "ainuo.zhiye.com": "beisen",
    "aosom1.zhiye.com": "beisen",
    "auxgroup.zhiye.com": "beisen",
    "awinic.zhiye.com": "beisen",
    "babybus.zhiye.com": "beisen",
    "bestechnic.zhiye.com": "beisen",
    "bozhon3.zhiye.com": "beisen",
    "campus.hundsun.com": "beisen",
    "campus.ke.com": "beisen",
    "canway.m.zhiye.com": "beisen",
    "career.h3c.com": "beisen",
    "career.hello-tech.com": "beisen",
    "careerktc.zhiye.com": "beisen",
    "cfmoto.zhiye.com": "beisen",
    "chinajack.m.zhiye.com": "beisen",
    "chipsea.zhiye.com": "beisen",
    "cht-group3.zhiye.com": "beisen",
    "cosmx.zhiye.com": "beisen",
    "csl-vacuum.zhiye.com": "beisen",
    "dahua1.zhiye.com": "beisen",
    "ddhd.cn": "beisen",
    "dioo.zhiye.com": "beisen",
    "efort.zhiye.com": "beisen",
    "eoptolink.zhiye.com": "beisen",
    "estun1.zhiye.com": "beisen",
    "fox-ess.zhiye.com": "beisen",
    "fscut.zhiye.com": "beisen",
    "fzzixun.zhiye.com": "beisen",
    "galaxea.zhiye.com": "beisen",
    "gccloud.zhiye.com": "beisen",
    "gdhw.zhiye.com": "beisen",
    "gravityxr.zhiye.com": "beisen",
    "haid1.zhiye.com": "beisen",
    "hand-china1.zhiye.com": "beisen",
    "hellobike.zhiye.com": "beisen",
    "hetao101.zhiye.com": "beisen",
    "hillstonenet.zhiye.com": "beisen",
    "hr-campus.vivo.com": "beisen",
    "huace.zhiye.com": "beisen",
    "iflytek.zhiye.com": "beisen",
    "ikingtec.zhiye.com": "beisen",
    "innosilicon.zhiye.com": "beisen",
    "intellif.zhiye.com": "beisen",
    "intsig.zhiye.com": "beisen",
    "invt.zhiye.com": "beisen",
    "jaka.zhiye.com": "beisen",
    "job.orbbec.com.cn": "beisen",
    "jssoft.zhiye.com": "beisen",
    "jtxzspace.zhiye.com": "beisen",
    "jwkaiwu2.zhiye.com": "beisen",
    "ldrobot.zhiye.com": "beisen",
    "leadchina.zhiye.com": "beisen",
    "leapmotor1.zhiye.com": "beisen",
    "lianheguangdian.zhiye.com": "beisen",
    "lisen.zhiye.com": "beisen",
    "longcheerzp1.zhiye.com": "beisen",
    "neusoft-campus.zhiye.com": "beisen",
    "nexchip.zhiye.com": "beisen",
    "novastar.zhiye.com": "beisen",
    "nvtpower.zhiye.com": "beisen",
    "oseasy.zhiye.com": "beisen",
    "pensun.zhiye.com": "beisen",
    "piotech.zhiye.com": "beisen",
    "pret.zhiye.com": "beisen",
    "pudutech.zhiye.com": "beisen",
    "qingmutec.zhiye.com": "beisen",
    "richsys1.zhiye.com": "beisen",
    "sailuntire.zhiye.com": "beisen",
    "sansec.zhiye.com": "beisen",
    "sany.zhiye.com": "beisen",
    "satlpec.zhiye.com": "beisen",
    "semi.zhiye.com": "beisen",
    "sf-auto.zhiye.com": "beisen",
    "shining3d.zhiye.com": "beisen",
    "shukun.zhiye.com": "beisen",
    "siasun.zhiye.com": "beisen",
    "siglent.zhiye.com": "beisen",
    "snowman.zhiye.com": "beisen",
    "sunseed.zhiye.com": "beisen",
    "szkingdom1.zhiye.com": "beisen",
    "sznari.zhiye.com": "beisen",
    "t-ray.zhiye.com": "beisen",
    "tica.zhiye.com": "beisen",
    "transsion.zhiye.com": "beisen",
    "tuniu.zhiye.com": "beisen",
    "ubtrobot.zhiye.com": "beisen",
    "unilumin.zhiye.com": "beisen",
    "vmax.zhiye.com": "beisen",
    "we.zyt.com": "beisen",
    "woanhome.zhiye.com": "beisen",
    "wondersharecampus.zhiye.com": "beisen",
    "xinje.zhiye.com": "beisen",
    "xynovatech.zhiye.com": "beisen",
    "yealink.zhiye.com": "beisen",
    "yeestor.zhiye.com": "beisen",
    "ymtc-campus.zhiye.com": "beisen",
    # beisen_mobile (1)
    "amedac.m.zhiye.com": "beisen_mobile",
    # bilibili (1)
    "jobs.bilibili.com": "bilibili",
    # bytedance (1)
    "jobs.bytedance.com": "bytedance",
    # chaitin (1)
    "join.chaitin.cn": "chaitin",
    # cvte (1)
    "campus.cvte.com": "cvte",
    # dekeinfo (1)
    "www.dekeinfo.com": "dekeinfo",
    # dji (1)
    "apply.careers.dji.com": "dji",
    # duoyi (1)
    "xz.duoyi.com": "duoyi",
    # elex (1)
    "elex-work.jobs.feishu.cn": "elex",
    # fanruan (1)
    "join.fanruan.com": "fanruan",
    # feishu (48)
    "acnri4vb8c0j.jobs.feishu.cn": "feishu",
    "agirobot.jobs.feishu.cn": "feishu",
    "anker-in.jobs.feishu.cn": "feishu",
    "boke.jobs.feishu.cn": "feishu",
    "campus.duxiaoman.com": "feishu",
    "career.papegames.com": "feishu",
    "dcar.jobs.feishu.cn": "feishu",
    "dexmal-inc.jobs.feishu.cn": "feishu",
    "echotech.jobs.feishu.cn": "feishu",
    "fcn5hvc5qbfs.jobs.feishu.cn": "feishu",
    "flexivrobotics.jobs.feishu.cn": "feishu",
    "geg7eg8cyc.jobs.feishu.cn": "feishu",
    "hf7l9aiqzx.jobs.feishu.cn": "feishu",
    "hr-jobs.sensetime.com": "feishu",
    "huayugames.jobs.feishu.cn": "feishu",
    "iucylxooqp.jobs.feishu.cn": "feishu",
    "jobs.66y.com": "feishu",
    "jzyxgames.jobs.feishu.cn": "feishu",
    "kargobot.jobs.feishu.cn": "feishu",
    "kurogame.jobs.feishu.cn": "feishu",
    "kwh0jtf778.jobs.feishu.cn": "feishu",
    "lilithgames.jobs.feishu.cn": "feishu",
    "mammotion.jobs.feishu.cn": "feishu",
    "meta.jobs.feishu.cn": "feishu",
    "momenta.jobs.feishu.cn": "feishu",
    "moonton.jobs.feishu.cn": "feishu",
    "nio.jobs.feishu.cn": "feishu",
    "nwd4iy9rd2s.jobs.feishu.cn": "feishu",
    "orinko-ht.jobs.feishu.cn": "feishu",
    "poizon.jobs.feishu.cn": "feishu",
    "ponyai.jobs.feishu.cn": "feishu",
    "primebot.jobs.feishu.cn": "feishu",
    "qcnhg4ksaiwt.jobs.feishu.cn": "feishu",
    "r3c0qt6yjw.jobs.feishu.cn": "feishu",
    "rastargame.jobs.feishu.cn": "feishu",
    "rcnrrgo8je7j.jobs.feishu.cn": "feishu",
    "tarsrobot.jobs.feishu.cn": "feishu",
    "uq1h428xyc.jobs.feishu.cn": "feishu",
    "varp4lp3dbc.jobs.feishu.cn": "feishu",
    "vrfi1sk8a0.jobs.feishu.cn": "feishu",
    "wdh.jobs.feishu.cn": "feishu",
    "wepie.jobs.feishu.cn": "feishu",
    "x2-robot.jobs.feishu.cn": "feishu",
    "xd-legacy.jobs.feishu.cn": "feishu",
    "xiaopeng.jobs.feishu.cn": "feishu",
    "yesv-desaysv.jobs.feishu.cn": "feishu",
    "yftech2012.jobs.feishu.cn": "feishu",
    "zj-innolight.jobs.feishu.cn": "feishu",
    # greenvalley (1)
    "campus.51job.com": "greenvalley",
    # gtasemi (1)
    "www.gtasemi.com.cn": "gtasemi",
    # hotjob (7)
    "ampace.hotjob.cn": "hotjob",
    "career.honor.com": "hotjob",
    "career.yonyou.com": "hotjob",
    "cgwx.hotjob.cn": "hotjob",
    "gcoreinc.hotjob.cn": "hotjob",
    "positec.hotjob.cn": "hotjob",
    "wecruit.hotjob.cn": "hotjob",
    # huawei (1)
    "career.huawei.com": "huawei",
    # isoftstone (1)
    "career.isoftstone.com": "isoftstone",
    # itek (1)
    "career.i-tek.cn": "itek",
    # jd (1)
    "campus.jd.com": "jd",
    # kuaishou (1)
    "campus.kuaishou.cn": "kuaishou",
    # leihuo (1)
    "leihuo.163.com": "leihuo",
    # lenovo (1)
    "talent.lenovo.com.cn": "lenovo",
    # lixiang (1)
    "www.lixiang.com": "lixiang",
    # meituan (1)
    "campus.meituan.com": "meituan",
    # mihoyo (1)
    "jobs.mihoyo.com": "mihoyo",
    # moka (9)
    "app-tc.mokahr.com": "moka",
    "app.mokahr.com": "moka",
    "app135149.dingtalkoxm.com": "moka",
    "campus.bigo.sg": "moka",
    "campus.geely.com": "moka",
    "hr.sundray.com.cn": "moka",
    "job.ronds.com": "moka",
    "talent.catl.com": "moka",
    "zhaopin.ninebot.com": "moka",
    # moseeker (1)
    "www.moseeker.com": "moseeker",
    # netease (2)
    "campus.163.com": "netease",
    "campus.game.163.com": "netease",
    # ourats (1)
    "job.skyverse.cn": "ourats",
    # pdd (1)
    "careers.pddglobalhr.com": "pdd",
    # qiyunfang (1)
    "www.qiyunfang.com": "qiyunfang",
    # tencent (1)
    "join.qq.com": "tencent",
    # tencent_music (1)
    "join.tencentmusic.com": "tencent_music",
    # unitree (1)
    "www.unitree.com": "unitree",
}


class OcEntryDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str
    entry_kind: Literal[
        "invalid_entry",
        "existing_adapter",
        "declarative_candidate",
        "python_candidate",
        "entry_discovery_required",
        "form_application",
    ]
    crawler_key: str | None = None
    candidate_kind: Literal["reuse", "declarative", "python"] | None = None
    reason: str


def diagnose_candidate_entry(url: str) -> OcEntryDiagnosis:
    """Classify an OC destination without fetching it or guessing from page content."""

    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError:
        return OcEntryDiagnosis(
            url=url, entry_kind="invalid_entry", reason="The destination URL is malformed.",
        )
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        return OcEntryDiagnosis(
            url=url,
            entry_kind="invalid_entry",
            reason="The destination is not an absolute HTTP(S) URL.",
        )
    if (
        hostname == "localhost" or hostname.endswith(".localhost")
        or hostname == "givemeoc.com" or hostname.endswith(".givemeoc.com")
    ):
        return OcEntryDiagnosis(
            url=url,
            entry_kind="invalid_entry",
            reason="OC signed links and loopback URLs are not crawler entries.",
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return OcEntryDiagnosis(
            url=url,
            entry_kind="invalid_entry",
            reason="Private and non-global IP destinations are not accepted.",
        )
    excluded = classify_oc_destination_url(url)
    if excluded is not None:
        kind, reason = excluded
        return OcEntryDiagnosis(
            url=url,
            entry_kind="form_application" if kind == "form" else "invalid_entry",
            reason=f"{kind}: {reason}",
        )
    form_hosts = ("kdocs.cn", "doc.weixin.qq.com", "wenjuan.com", "wj.toutiao.com", "docs.popo.163.com", "bsurl.cn")
    if any(hostname == host or hostname.endswith(f".{host}") for host in form_hosts):
        return OcEntryDiagnosis(
            url=url, entry_kind="form_application",
            reason="The form/document destination is not a crawlable job listing.",
        )
    route = f"{parsed.path}#{parsed.fragment}".casefold()
    if re.search(r"(?:^|[/#])(?:login|signin|captcha)(?:[/?#.]|$)", route):
        return OcEntryDiagnosis(
            url=url, entry_kind="invalid_entry",
            reason="login_page: Access-controlled entries cannot enumerate public jobs.",
        )
    # Check companies.yaml host→crawler mapping first
    if (parsed.scheme == "https" and parsed.netloc == "hr.tp-link.com.cn"
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment):
        return OcEntryDiagnosis(
            url=url, entry_kind="existing_adapter", crawler_key="tplink",
            candidate_kind="reuse", reason="The exact public TP-LINK campus home has a verified home-data adapter.",
        )
    crawler_key = _COMPANIES_YAML_HOST_MAP.get(hostname)
    if crawler_key is not None:
        return OcEntryDiagnosis(
            url=url,
            entry_kind="existing_adapter",
            crawler_key=crawler_key,
            candidate_kind="reuse",
            reason=f"The destination matches the {crawler_key} adapter from companies.yaml.",
        )
    path = parsed.path.casefold()
    crawler_key = None
    if (
        hostname == "campus.10jqka.com.cn"
        and (
            path.rstrip("/") == "/mobile/job/list"
            or (
                path.rstrip("/") == "/job/list"
                and "sid=" in parsed.query.casefold()
            )
        )
    ):
        crawler_key = "tonghuashun"
    elif re.search(r"/(?:campus_apply|campus-recruitment)/", path):
        crawler_key = "moka"
    elif path.startswith("/campus/position") and "spread=" in parsed.query.casefold():
        crawler_key = "feishu"
    elif hostname == "app.mokahr.com" or hostname.endswith(".mokahr.com"):
        crawler_key = "moka"
    elif hostname.endswith(".jobs.feishu.cn"):
        crawler_key = "feishu"
    elif hostname.endswith(".m.zhiye.com"):
        crawler_key = "beisen_mobile"
    elif hostname.endswith(".zhiye.com"):
        crawler_key = "beisen"
    elif hostname.endswith(".hotjob.cn"):
        crawler_key = "hotjob"
    elif hostname.endswith(".moseeker.com") or hostname == "moseeker.com":
        crawler_key = "moseeker"
    if crawler_key is not None:
        return OcEntryDiagnosis(
            url=url,
            entry_kind="existing_adapter",
            crawler_key=crawler_key,
            candidate_kind="reuse",
            reason=f"The destination matches the reusable {crawler_key} ATS adapter.",
        )

    recruitment_hint = re.search(
        r"career|campus|recruit|job|join|zhaopin|position|talent|hr", f"{hostname} {route}",
    )
    if not recruitment_hint:
        return OcEntryDiagnosis(
            url=url,
            entry_kind="entry_discovery_required",
            crawler_key="render",
            reason="The destination does not identify a deterministic recruitment listing.",
        )
    if re.search(r"/api(?:/|$)|\.json$", path) or any(
        marker in path for marker in ("/jobs", "/positions", "/position", "/post")
    ):
        return OcEntryDiagnosis(
            url=url,
            entry_kind="declarative_candidate",
            crawler_key="render",
            candidate_kind="declarative",
            reason=(
                "The custom listing is suitable for a fixture-backed "
                "declarative rule candidate."
            ),
        )
    return OcEntryDiagnosis(
        url=url,
        entry_kind="declarative_candidate",
        crawler_key="render",
        candidate_kind="declarative",
        reason="The custom recruitment entry needs a bounded declarative recipe candidate.",
    )


def infer_candidate_crawler(url: str) -> str | None:
    """Compatibility crawler inference; use diagnosis for adapter decisions."""

    diagnosis = diagnose_candidate_entry(url)
    return diagnosis.crawler_key
