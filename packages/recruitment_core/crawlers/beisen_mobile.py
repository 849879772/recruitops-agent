"""北森移动版 LightBolt 招聘 API crawler。

LightBolt 的移动端首页用 ``List`` 获取第一页，滚动加载用
``SearchJobAd`` 分页；详情页的完整职责和要求需要额外调用 ``Info``。
"""

import json
import logging
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class BeisenMobileCrawler(BaseCrawler):
    """Reusable crawler for ``*.m.zhiye.com`` LightBolt portals."""

    PAGE_SIZE = 10
    API_RETRIES = 3
    API_RETRY_BACKOFF_SECONDS = 0.5
    JD_RAW_LIMIT = 12000

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.pagination_complete = False
        self.expected_total: int | None = None
        self.termination = "not_started"
        # Keep the repository's existing crawler status attribute as well.
        self.pagination_termination_reason = self.termination

    @property
    def _origin(self) -> str:
        parsed = urlsplit(self.careers_url)
        return f"{parsed.scheme or 'https'}://{parsed.netloc}"

    @property
    def _route_params(self) -> dict[str, str]:
        parsed = urlsplit(self.careers_url)
        fragment = parsed.fragment or ""
        _, _, fragment_query = fragment.partition("?")
        query = fragment_query or parsed.query
        return dict(parse_qsl(query, keep_blank_values=True))

    @property
    def _from_route(self) -> str:
        return urlsplit(self.careers_url).fragment

    def _query_value(self, *names: str, default: str = "") -> str:
        params = self._route_params
        for name in names:
            value = params.get(name)
            if value is not None:
                return value
        return default

    def _jc(self) -> str:
        return self._query_value("jc", default="2") or "2"

    def _reward_job(self) -> str:
        return self._query_value("rewardjob", "RewardJob", default="0") or "0"

    def _list_params(self) -> dict[str, str]:
        return {
            "jc": self._jc(),
            "jobads": self._query_value("jobads"),
            "code": self._query_value("code"),
            "c1": self._query_value("c1"),
            "c2": self._query_value("c2"),
            "ky": self._query_value("ky"),
            "c": self._query_value("c"),
            "ct": self._query_value("ct"),
            "o": self._query_value("o"),
            "rewardjob": self._reward_job(),
            "shareid": self._query_value("shareid"),
            "token": self._query_value("token"),
            "shopid": self._query_value("shopid"),
            "From": self._from_route,
        }

    def _search_params(self, page: int) -> dict[str, str]:
        params = self._list_params()
        params.pop("From", None)
        params.update(
            {
                "jid": params["jobads"],
                "pi": str(page),
                "ps": str(self.PAGE_SIZE),
            }
        )
        return params

    def _detail_url(self, job_ad_id: str) -> str:
        return (
            f"{self._origin}/#/jobdetail?id={job_ad_id}"
            f"&jc={self._jc()}&isReward=false"
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.careers_url.split("#", 1)[0],
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        }

    @staticmethod
    def _response_json(response: requests.Response) -> dict[str, Any]:
        """Decode raw UTF-8 first; some tenants omit a JSON charset."""
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            for encoding in ("utf-8-sig", "utf-8"):
                try:
                    value = json.loads(bytes(content).decode(encoding))
                    if isinstance(value, dict):
                        return value
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("北森 API 返回的 JSON 不是对象")
        return value

    @classmethod
    def _response_code(cls, payload: dict[str, Any]) -> str:
        return str(payload.get("Code") or payload.get("code") or "")

    def _request_json(
        self,
        session: requests.Session,
        path: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        url = f"{self._origin}{path}"
        last_error: Exception | None = None
        for attempt in range(1, self.API_RETRIES + 1):
            try:
                response = session.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=25,
                )
                response.raise_for_status()
                payload = self._response_json(response)
                if self._response_code(payload) != "200":
                    raise RuntimeError(
                        payload.get("Message") or payload.get("message")
                        or "北森 LightBolt API 返回非 200"
                    )
                return payload
            except Exception as exc:
                last_error = exc
                if attempt == self.API_RETRIES:
                    break
                time.sleep(self.API_RETRY_BACKOFF_SECONDS * attempt)
                logger.warning(
                    "[%s] LightBolt 请求失败，重试 %d/%d：%s",
                    self.company_name,
                    attempt,
                    self.API_RETRIES - 1,
                    exc,
                )
        raise RuntimeError(f"LightBolt API 请求失败：{path}") from last_error

    @staticmethod
    def _page_data(payload: dict[str, Any]) -> dict[str, Any]:
        outer = payload.get("Data") or payload.get("data") or {}
        if not isinstance(outer, dict):
            return {}
        page = outer.get("data") or outer.get("Data") or outer
        return page if isinstance(page, dict) else {}

    @classmethod
    def _page_rows(cls, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = cls._page_data(payload).get("DataResult") or []
        return [row for row in rows if isinstance(row, dict)]

    @classmethod
    def _row_count(cls, payload: dict[str, Any]) -> int | None:
        value = cls._page_data(payload).get("RowCount")
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    def _info_payload(
        self,
        session: requests.Session,
        job_ad_id: str,
    ) -> dict[str, Any]:
        params = {
            "adid": job_ad_id,
            "shareid": self._query_value("shareid"),
            "token": self._query_value("token"),
            "From": self._from_route,
        }
        payload = self._request_json(session, "/LightBoltAPI/JobAd/Info", params)
        data = payload.get("Data") or payload.get("data") or {}
        return data if isinstance(data, dict) else {}

    def _make_lightbolt_job(
        self,
        session: requests.Session,
        row: dict[str, Any],
    ) -> dict[str, Any] | None:
        job_ad_id = self._text(row.get("JobAdId") or row.get("Id"))
        title = self._text(row.get("JobAdName") or row.get("title"))
        if not job_ad_id or not title:
            return None

        info = self._info_payload(session, job_ad_id)
        duty = self._text(info.get("DutyStr") or info.get("Duty"))
        require = self._text(info.get("RequireStr") or info.get("Require"))
        if not duty:
            duty = self._text(row.get("Duty"))
        if not require:
            require = self._text(row.get("Require"))

        jd_parts: list[str] = []
        if duty:
            jd_parts.extend(["岗位职责", duty])
        if require:
            jd_parts.extend(["任职要求", require])
        return self._make_job(
            title=title,
            city=self._text(info.get("LocName") or row.get("LocIdName")),
            job_type="校招",
            jd_url=self._detail_url(job_ad_id),
            jd_raw="\n".join(jd_parts)[: self.JD_RAW_LIMIT],
            published_at=self._text(
                info.get("PostDateStr") or row.get("ToPostDate")
            ),
            link_kind="detail",
        )

    def _set_termination(self, value: str, complete: bool) -> None:
        self.pagination_complete = complete
        self.termination = value
        self.pagination_termination_reason = value

    def fetch(self) -> list[dict]:
        self._set_termination("not_started", False)
        self.expected_total = None
        session = requests.Session()
        rows_by_id: dict[str, dict[str, Any]] = {}
        list_error: Exception | None = None

        try:
            payload = self._request_json(
                session,
                "/LightBoltAPI/JobAd/List",
                self._list_params(),
            )
        except Exception as exc:
            list_error = exc
            logger.warning("[%s] LightBolt List 失败，改用 SearchJobAd：%s", self.company_name, exc)
            payload = None

        if payload is None:
            try:
                payload = self._request_json(
                    session,
                    "/LightBoltAPI/JobAd/SearchJobAd",
                    self._search_params(1),
                )
            except Exception as exc:
                logger.error("[%s] LightBolt 首页失败：%s", self.company_name, exc)
                self._set_termination("first_page_failed", False)
                return []

        self.expected_total = self._row_count(payload)
        for row in self._page_rows(payload):
            job_ad_id = self._text(row.get("JobAdId") or row.get("Id"))
            if job_ad_id:
                rows_by_id.setdefault(job_ad_id, row)

        if self.expected_total is None:
            self._set_termination("rowcount_missing", False)
            return []
        if self.expected_total == 0:
            self._set_termination("rowcount_zero", True)
            return []

        page = 2
        while len(rows_by_id) < self.expected_total:
            try:
                payload = self._request_json(
                    session,
                    "/LightBoltAPI/JobAd/SearchJobAd",
                    self._search_params(page),
                )
            except Exception as exc:
                logger.error("[%s] LightBolt 第 %d 页失败：%s", self.company_name, page, exc)
                self._set_termination(f"search_page_{page}_failed", False)
                break

            reported_total = self._row_count(payload)
            if reported_total is not None:
                self.expected_total = reported_total
            rows = self._page_rows(payload)
            if not rows:
                self._set_termination("search_empty_before_rowcount", False)
                break
            for row in rows:
                job_ad_id = self._text(row.get("JobAdId") or row.get("Id"))
                if job_ad_id:
                    rows_by_id.setdefault(job_ad_id, row)
            page += 1

        if len(rows_by_id) != self.expected_total:
            if self.pagination_termination_reason == "not_started":
                self._set_termination("rowcount_mismatch", False)
            return self._hydrate_jobs(session, rows_by_id)

        jobs = self._hydrate_jobs(session, rows_by_id)
        if len(jobs) != self.expected_total:
            self._set_termination("info_incomplete", False)
            return jobs

        self._set_termination("rowcount_reached", True)
        logger.info(
            "[%s] LightBolt 完整抓取 %d/%d 个岗位%s",
            self.company_name,
            len(jobs),
            self.expected_total,
            "（List 失败后 SearchJobAd 接管）" if list_error else "",
        )
        return jobs

    def _hydrate_jobs(
        self,
        session: requests.Session,
        rows_by_id: dict[str, dict[str, Any]],
    ) -> list[dict]:
        jobs: list[dict] = []
        for row in rows_by_id.values():
            try:
                job = self._make_lightbolt_job(session, row)
            except Exception as exc:
                logger.warning("[%s] LightBolt Info 补全失败：%s", self.company_name, exc)
                continue
            if job:
                jobs.append(job)
        return jobs


# Descriptive aliases keep direct imports convenient without changing the
# repository-wide crawler registry, which is intentionally outside this task.
BeisenMobileLightBoltCrawler = BeisenMobileCrawler
BeisenLightBoltCrawler = BeisenMobileCrawler
