"""CVTE campus crawler for the Next.js campus API."""
import logging
import math
from collections.abc import Mapping
from urllib.parse import parse_qs, quote, urlsplit

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class CVTECrawler(BaseCrawler):
    PROJECTS_API = "https://campus.cvte.com/api/project"
    POSITIONS_API = "https://campus.cvte.com/api/position"
    JD_RAW_LIMIT = 12000

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_evidence()

    def _reset_evidence(self) -> None:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.fetch_failed = False
        self.project_diagnostics = []
        self.requested_project_id = self._project_id(self.careers_url)
        self.scope_changed = False
        self.scope_old_project_id = self.requested_project_id
        self.scope_original_project_id = self.requested_project_id
        self.scope_observed_project_id = ""
        self.scope_observed_project_ids = []
        self.scope_observed_project_name = ""
        self.scope_observation_status = "not_checked"

    @staticmethod
    def _project_id(url: str) -> str:
        parts = [part for part in urlsplit(url or "").path.split("/") if part]
        if len(parts) == 2 and parts[0].casefold() == "project":
            return parts[1].strip()
        return ""

    @staticmethod
    def _as_int(value: object) -> int | None:
        if isinstance(value, bool) or value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _project_not_found_payload(payload: object) -> bool:
        if not isinstance(payload, Mapping):
            return False
        message = str(
            payload.get("msg") or payload.get("message") or payload.get("error") or ""
        ).strip().casefold()
        return "无此项目" in message or "project not found" in message

    @staticmethod
    def _position_scope_from_url(request_url: str) -> dict[str, object]:
        parsed = urlsplit(request_url or "")
        if (
            parsed.scheme.casefold() != "https"
            or parsed.netloc.casefold() != "campus.cvte.com"
            or parsed.path != "/api/position"
        ):
            return {"project_ids": [], "request_url": ""}

        project_ids: list[str] = []
        for raw_value in parse_qs(parsed.query).get("projectIds", []):
            for value in raw_value.split(","):
                value = value.strip()
                if value and value not in project_ids:
                    project_ids.append(value)
        return {"project_ids": project_ids, "request_url": request_url}

    @staticmethod
    def _project_name(item: Mapping[str, object]) -> str:
        for key in (
            "name",
            "projectName",
            "project_name",
            "title",
            "recruitName",
            "projectTitle",
        ):
            value = item.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    @classmethod
    def _metadata_value(cls, payload: Mapping[str, object], keys: tuple[str, ...]) -> object:
        containers: list[Mapping[str, object]] = [payload]
        for key in ("pagination", "pageInfo", "data"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
        for container in containers:
            for key in keys:
                if key in container:
                    return container[key]
        return None

    @staticmethod
    def _as_bool(value: object) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "1", "yes"}:
                return True
            if normalized in {"false", "0", "no"}:
                return False
        return None

    @classmethod
    def _row_project_id(cls, item: Mapping[str, object]) -> str:
        for key in ("projectId", "projectID", "project_id"):
            if item.get(key) not in (None, ""):
                return str(item[key]).strip()
        project = item.get("project")
        if isinstance(project, Mapping) and project.get("id") not in (None, ""):
            return str(project["id"]).strip()
        return ""

    def _confirm_project_missing(
        self, project_id: str, headers: dict[str, str]
    ) -> dict[str, object]:
        detail_url = f"{self.PROJECTS_API}/{quote(project_id, safe='')}"
        try:
            response = requests.get(detail_url, headers=headers, timeout=30)
            try:
                payload = response.json()
            except (TypeError, ValueError):
                payload = None
            status_code = getattr(response, "status_code", 200)
            if self._project_not_found_payload(payload) or status_code == 404:
                return {"status": "missing", "reason": "project_not_found"}
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[%s] CVTE project lookup failed project=%s: %s",
                self.company_name,
                project_id,
                exc,
            )
            return {"status": "failed", "reason": "project_scope_lookup_failed"}
        return {"status": "valid", "reason": "project_exists"}

    def _project_catalog_names(self, headers: dict[str, str]) -> dict[str, str]:
        try:
            response = requests.get(self.PROJECTS_API, headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            projects = payload.get("projects") if isinstance(payload, Mapping) else None
            if not isinstance(projects, list):
                return {}
        except Exception as exc:  # noqa: BLE001
            logger.info("[%s] CVTE project names unavailable: %s", self.company_name, exc)
            return {}

        names = {}
        for project in projects:
            if not isinstance(project, Mapping) or project.get("id") in (None, ""):
                continue
            names[str(project["id"]).strip()] = self._project_name(project)
        return names

    def _observe_page_scope(self) -> dict[str, object]:
        empty = {
            "status": "unknown",
            "reason": "scope_observation_missing",
            "project_ids": [],
            "request_url": "",
            "project_name": "",
        }
        page_url = urlsplit(self.careers_url or "")
        if (
            page_url.scheme.casefold() != "https"
            or page_url.netloc.casefold() != "campus.cvte.com"
            or not self.requested_project_id
        ):
            empty["reason"] = "scope_page_url_not_allowed"
            return empty

        try:
            from playwright.sync_api import TimeoutError as PWTimeout
            from playwright.sync_api import sync_playwright

            from .base import launch_browser
        except ImportError:
            empty["status"] = "failed"
            empty["reason"] = "playwright_unavailable"
            return empty

        latest: dict[str, object] = {}

        def on_request(request) -> None:
            nonlocal latest
            observed = self._position_scope_from_url(request.url)
            if observed["project_ids"]:
                latest = observed

        try:
            with sync_playwright() as playwright:
                browser = launch_browser(playwright, headless=True)
                try:
                    page = browser.new_page()
                    page.on("request", on_request)
                    try:
                        page.goto(
                            self.careers_url,
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                    except PWTimeout:
                        pass
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except PWTimeout:
                        pass
                    if not latest:
                        page.wait_for_timeout(1500)
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            if not latest:
                logger.warning(
                    "[%s] CVTE scope observation failed: %s", self.company_name, exc
                )
                empty["status"] = "failed"
                empty["reason"] = "scope_observation_failed"
                return empty

        if not latest:
            return empty
        return {
            "status": "observed",
            "reason": "position_scope_observed",
            "project_ids": list(latest["project_ids"]),
            "request_url": latest["request_url"],
            "project_name": "",
        }

    def _recover_page_scope(
        self, old_project_id: str, headers: dict[str, str]
    ) -> dict[str, object]:
        confirmation = self._confirm_project_missing(old_project_id, headers)
        if confirmation["status"] != "missing":
            return confirmation

        observation = self._observe_page_scope()
        observed_ids = [
            str(project_id).strip()
            for project_id in observation.get("project_ids", [])
            if str(project_id).strip()
        ]
        if not observed_ids or all(project_id == old_project_id for project_id in observed_ids):
            return {
                "status": observation.get("status", "unknown"),
                "reason": observation.get("reason", "scope_observation_missing"),
                "project_ids": [],
                "project_name": "",
                "request_url": observation.get("request_url", ""),
            }

        names = self._project_catalog_names(headers)
        project_name = str(observation.get("project_name") or "").strip()
        if not project_name:
            project_name = names.get(observed_ids[0], "")
        return {
            "status": "changed",
            "reason": "scope_changed",
            "project_ids": list(dict.fromkeys(observed_ids)),
            "project_name": project_name,
            "request_url": observation.get("request_url", ""),
        }

    def _fetch_project(self, project_id: str, headers: dict[str, str]) -> dict[str, object]:
        evidence: dict[str, object] = {
            "project_id": project_id,
            "jobs": [],
            "pages_seen": 0,
            "total_pages": None,
            "advertised_total": None,
            "observed_unique": 0,
            "has_more": False,
            "complete": False,
            "fetch_failed": False,
            "reason": "not_started",
        }
        try:
            resp = requests.get(
                self.POSITIONS_API,
                params={"projectIds": project_id},
                headers=headers,
                timeout=30,
            )
            payload = resp.json()
            if not isinstance(payload, Mapping):
                raise ValueError("positions_payload_not_object")
            if self._project_not_found_payload(payload):
                evidence["reason"] = "project_not_found"
                return evidence
            resp.raise_for_status()
            rows = payload.get("projectPositions")
            if not isinstance(rows, list):
                raise ValueError("project_positions_missing")
        except Exception as exc:  # noqa: BLE001
            evidence["fetch_failed"] = True
            evidence["reason"] = "positions_request_failed"
            logger.warning(
                "[%s] CVTE positions API failed project=%s: %s",
                self.company_name,
                project_id,
                exc,
            )
            return evidence

        evidence["pages_seen"] = 1
        advertised_total = self._as_int(
            self._metadata_value(payload, ("total", "totalCount", "count", "totalNum"))
        )
        page_size = self._as_int(self._metadata_value(payload, ("pageSize", "size", "limit")))
        reported_pages = self._as_int(
            self._metadata_value(payload, ("totalPages", "pageCount", "pages"))
        )
        if reported_pages is not None and reported_pages > 0:
            total_pages = reported_pages
        elif advertised_total is not None and page_size:
            total_pages = max(1, math.ceil(advertised_total / page_size))
        else:
            total_pages = 1
        evidence["total_pages"] = total_pages

        # /api/position returns one project-scoped collection rather than a page
        # cursor.  The returned collection is the platform's unpaginated contract.
        single_response_contract = total_pages == 1 and not page_size
        evidence["advertised_total"] = advertised_total
        reported_has_more = self._as_bool(
            self._metadata_value(payload, ("hasMore", "has_more"))
        )
        has_more = (
            reported_has_more
            if reported_has_more is not None
            else evidence["pages_seen"] < total_pages
        )
        evidence["has_more"] = has_more

        jobs, seen = [], set()
        scope_violation = False
        duplicate_id = False
        invalid_row = False
        for item in rows:
            if not isinstance(item, Mapping):
                invalid_row = True
                continue
            row_project_id = self._row_project_id(item)
            if row_project_id and row_project_id != project_id:
                scope_violation = True
                continue
            jid = str(item.get("id") or "")
            title = item.get("name") or item.get("positionName") or ""
            if not title or not jid:
                invalid_row = True
                continue
            if jid in seen:
                duplicate_id = True
                continue
            seen.add(jid)
            areas = item.get("areaViews") or []
            city = "、".join(
                str(area.get("cityName", "")) for area in areas if isinstance(area, Mapping)
            )[:40]
            jd_raw = "\n".join(
                str(x) for x in [item.get("duty") or "", item.get("requirement") or ""] if x
            )[: self.JD_RAW_LIMIT]
            jobs.append(
                self._make_job(
                    title=title,
                    city=city,
                    jd_url=f"https://campus.cvte.com/position/{jid}",
                    jd_raw=jd_raw,
                )
            )

        evidence["observed_unique"] = len(seen)
        if scope_violation:
            reason = "project_scope_mismatch"
        elif has_more or evidence["pages_seen"] < total_pages:
            reason = "pagination_not_exhausted"
        elif duplicate_id:
            reason = "duplicate_job_id"
        elif invalid_row:
            reason = "invalid_row"
        elif advertised_total is None and single_response_contract:
            evidence["complete"] = True
            reason = "single_project_response"
        elif advertised_total is None:
            reason = "missing_total"
        elif len(seen) != advertised_total:
            reason = "advertised_total_mismatch"
        else:
            evidence["complete"] = True
            reason = "api_total_reached"
        evidence["reason"] = reason
        evidence["jobs"] = jobs
        return evidence

    def fetch(self) -> list[dict]:
        self._reset_evidence()
        project_id = self.requested_project_id
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://campus.cvte.com/position"}
        if project_id:
            initial_run = self._fetch_project(project_id, headers)
            runs = [initial_run]
            needs_scope_recovery = (
                initial_run["reason"] == "project_not_found"
                or (
                    not initial_run["fetch_failed"]
                    and not initial_run["observed_unique"]
                    and initial_run["reason"] in {"single_project_response", "api_total_reached"}
                )
            )
            if needs_scope_recovery:
                resolution = self._recover_page_scope(project_id, headers)
                self.scope_observation_status = str(resolution.get("status", "unknown"))
                if resolution.get("status") == "changed":
                    observed_ids = [
                        str(value).strip()
                        for value in resolution.get("project_ids", [])
                        if str(value).strip()
                    ]
                    self.scope_changed = bool(observed_ids)
                    self.scope_observed_project_ids = list(dict.fromkeys(observed_ids))
                    self.scope_observed_project_id = ",".join(
                        self.scope_observed_project_ids
                    )
                    self.scope_observed_project_name = str(
                        resolution.get("project_name") or ""
                    ).strip()
                    runs = [
                        self._fetch_project(current_id, headers)
                        for current_id in self.scope_observed_project_ids
                    ]
                elif not (resolution.get("status") == "valid" and initial_run["complete"]):
                    initial_run["complete"] = False
                    initial_run["has_more"] = True
                    initial_run["reason"] = str(
                        resolution.get("reason") or "scope_observation_unknown"
                    )
                    if resolution.get("status") == "failed":
                        initial_run["fetch_failed"] = True
        else:
            try:
                projects_resp = requests.get(self.PROJECTS_API, headers=headers, timeout=30)
                projects_resp.raise_for_status()
                projects_payload = projects_resp.json()
                if not isinstance(projects_payload, Mapping):
                    raise ValueError("projects_payload_not_object")
                projects = projects_payload.get("projects")
                if not isinstance(projects, list):
                    raise ValueError("projects_missing")
                project_ids = list(dict.fromkeys(
                    str(project.get("id")).strip()
                    for project in projects
                    if isinstance(project, Mapping) and project.get("id") not in (None, "")
                ))
            except Exception as exc:  # noqa: BLE001
                self.fetch_failed = True
                self.pagination_termination_reason = "projects_request_failed"
                logger.warning("[%s] CVTE projects API failed: %s", self.company_name, exc)
                return []

            if not project_ids:
                self.project_diagnostics = []
                self.pagination_complete = True
                self.pagination_termination_reason = "no_active_projects"
                logger.info("[%s] CVTE no active project positions", self.company_name)
                return []
            runs = [self._fetch_project(current_id, headers) for current_id in project_ids]
        jobs, seen_jobs = [], set()
        for run in runs:
            for job in run["jobs"]:
                identity = str(job.get("jd_url") or "")
                if identity in seen_jobs:
                    continue
                seen_jobs.add(identity)
                jobs.append(job)

        self.project_diagnostics = [
            {
                "project_id": run["project_id"],
                "pagination_complete": bool(run["complete"]),
                "pages_seen": int(run["pages_seen"]),
                "total_pages": run["total_pages"],
                "advertised_total": run["advertised_total"],
                "observed_unique": int(run["observed_unique"]),
                "has_more": bool(run["has_more"]),
                "termination_reason": str(run["reason"]),
                "fetch_failed": bool(run["fetch_failed"]),
                "scope_changed": bool(self.scope_changed),
                "old_project_id": self.scope_old_project_id if project_id else "",
                "observed_project_id": self.scope_observed_project_id,
                "observed_project_name": self.scope_observed_project_name,
            }
            for run in runs
        ]
        self.pages_seen = sum(int(run["pages_seen"]) for run in runs)
        run_pages = [run["total_pages"] for run in runs]
        self.total_pages = (
            sum(int(value) for value in run_pages)
            if run_pages and all(value is not None for value in run_pages)
            else None
        )
        run_totals = [run["advertised_total"] for run in runs]
        self.advertised_total = (
            run_totals[0] if project_id and len(run_totals) == 1 else None
        )
        self.has_more = any(bool(run["has_more"]) for run in runs)
        self.fetch_failed = any(bool(run["fetch_failed"]) for run in runs)
        self.pagination_complete = bool(runs) and all(
            bool(run["complete"]) for run in runs
        ) and not self.fetch_failed
        if self.pagination_complete:
            self.has_more = False
            self.pagination_termination_reason = (
                "api_total_reached" if project_id else "all_active_projects_complete"
            )
        else:
            self.pagination_termination_reason = next(
                (
                    str(run["reason"])
                    for run in runs
                    if not run["complete"] and run["reason"] != "not_started"
                ),
                "pagination_incomplete",
            )

        logger.info("[%s] CVTE caught %d jobs", self.company_name, len(jobs))
        return jobs
