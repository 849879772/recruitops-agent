"""Bounded, read-only selection and matching of OC crawl evaluation artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from packages.discovery import (
    SourceLead,
    consolidate_source_leads,
    normalize_company_name,
    normalize_oc_destination_url,
)
from scripts.run_oc_full_crawl_eval import _lead_key

MAX_EVALUATION_FILES = 64
MAX_DIRECTORY_ENTRIES = 2048
MAX_EVALUATION_BYTES = 64 * 1024 * 1024
MAX_TOTAL_EVALUATION_BYTES = 512 * 1024 * 1024
MAX_EVALUATION_RESULTS = 10_000
MAX_HEADER_CHARS = 65_536

_RESULT_FIELDS = frozenset({
    "lead_key", "company", "source_projects", "source_url", "original_source_url",
    "original_url", "discovered_entry_url", "canonical_url", "crawl_source_url",
    "effective_source_urls", "source_runs", "crawler_key",
    "integration_status", "error_code", "completed_at", "accepted_count",
    "raw_job_count", "complete_jd_count", "incomplete_jd_count",
    "pagination_complete", "completeness_known", "has_more", "pages_seen",
    "total_pages", "advertised_total", "termination_reasons",
})


def evaluation_recency(row: Mapping[str, Any], order: int = 0) -> tuple[int, float, int]:
    value = str(row.get("completed_at") or "").strip()
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return (1, timestamp.timestamp(), order)
    except (ValueError, OverflowError, OSError):
        return (0, 0.0, order)


def evaluation_urls(row: Mapping[str, Any]) -> list[str]:
    values: list[object] = [
        row.get(field) for field in (
            "original_source_url", "original_url", "source_url",
            "discovered_entry_url", "crawl_source_url", "canonical_url",
        )
    ]
    values.extend(row.get("effective_source_urls") or ())
    for run in row.get("source_runs") or ():
        if isinstance(run, Mapping):
            values.extend(
                run.get(field) for field in (
                    "source_url", "original_source_url", "discovered_entry_url",
                    "crawl_source_url", "effective_source_url",
                )
            )
            values.extend(run.get("effective_source_urls") or ())
    return list(dict.fromkeys(
        url for value in values
        if (url := normalize_oc_destination_url(value))
    ))


def canonical_evaluation_url(row: Mapping[str, Any]) -> str:
    return normalize_oc_destination_url(
        row.get("crawl_source_url")
        or row.get("discovered_entry_url")
        or row.get("canonical_url")
        or row.get("source_url")
    )


def _project_names(lead: SourceLead) -> list[str]:
    return list(lead.metadata.get("source_project_names") or [lead.canonical_name])


def _primary_url(lead: SourceLead) -> str:
    # Keep the source binding identical to the resumable evaluator's lead key.
    from packages.tools.oc_candidates import infer_candidate_crawler

    return next((url for url in lead.source_urls if infer_candidate_crawler(url) is not None), "")


class EvaluationIndex:
    """Match exact source URLs and evaluator lead keys without merging ATS tenants."""

    def __init__(self, rows: Iterable[Mapping[str, Any]], leads: Iterable[SourceLead]):
        self.by_url: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self.by_key: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        original_leads = list(leads)
        keyed_leads = {
            _lead_key(lead): lead
            for lead in [*original_leads, *consolidate_source_leads(original_leads)]
        }
        source_owners: dict[str, set[str]] = {}
        for lead in original_leads:
            for url in lead.source_urls:
                source_owners.setdefault(normalize_oc_destination_url(url), set()).add(
                    normalize_company_name(lead.canonical_name)
                )
        alias_origins: dict[str, set[tuple[str, str]]] = {}
        ordered = sorted(enumerate(rows), key=lambda item: evaluation_recency(item[1], item[0]))
        for order, item in ordered:
            row = dict(item)
            key = str(row.get("lead_key") or "")
            keyed_lead = keyed_leads.get(key)
            urls = evaluation_urls(row)
            original = normalize_oc_destination_url(row.get("original_source_url") or row.get("original_url"))
            source_url = normalize_oc_destination_url(row.get("source_url"))
            if keyed_lead is not None:
                primary = _primary_url(keyed_lead)
                # A keyed redirect binds back to the original URL, not every
                # campaign URL that happened to be consolidated into this lead.
                if primary and not original and source_url not in keyed_lead.source_urls:
                    original = primary
                    row["original_source_url"] = original
                    urls.append(original)
                if not row.get("source_projects") and (
                    not row.get("company") or normalize_company_name(row["company"]) in {
                        normalize_company_name(name) for name in _project_names(keyed_lead)
                    }
                ):
                    row["source_projects"] = _project_names(keyed_lead)
            scope = {
                normalize_company_name(name)
                for name in row.get("source_projects") or [row.get("company")]
                if normalize_company_name(name)
            }
            anchors = [original or source_url]
            for run in row.get("source_runs") or ():
                if isinstance(run, Mapping):
                    anchors.append(normalize_oc_destination_url(
                        run.get("original_source_url") or run.get("source_url")
                    ))
            bindings: set[tuple[str, str]] = set()
            for anchor in anchors:
                direct = {(name, anchor) for name in source_owners.get(anchor, set())}
                inherited = alias_origins.get(anchor, set()) if not original else set()
                candidates = direct | inherited
                if scope:
                    candidates = {pair for pair in candidates if pair[0] in scope}
                # Anonymous legacy retries must identify exactly one source.
                # Named results may explicitly cover a shared original URL.
                if len(candidates) == 1:
                    bindings.update(candidates)
                elif scope and direct:
                    bindings.update(pair for pair in direct if pair[0] in scope)
            if len({url for _, url in bindings}) == 1 and not original:
                origin = next(iter(bindings))[1]
                if origin != source_url:
                    row["original_source_url"] = origin
            if bindings:
                for alias in urls:
                    alias_origins.setdefault(alias, set()).update(bindings)
            if key:
                self.by_key.setdefault(key, []).append((order, row))
            # Effective URLs are redirect evidence, not independent acceptance
            # of every other project that happens to use that destination.
            for origin in {url for _, url in bindings}:
                bound = dict(row, source_projects=sorted(name for name, url in bindings if url == origin))
                self.by_url.setdefault(origin, []).append((order, bound))

    def for_source(self, lead: SourceLead, url: str) -> dict[str, Any] | None:
        normalized = normalize_oc_destination_url(url)
        matches = list(self.by_url.get(normalized, []))
        if not normalized:
            matches.extend(self.by_key.get(_lead_key(lead), []))
        scoped = []
        names = {normalize_company_name(name) for name in _project_names(lead)}
        for order, row in matches:
            projects = row.get("source_projects") or []
            # Legacy URL-only fixtures have no project scope and remain valid.
            if projects and not names.intersection(normalize_company_name(item) for item in projects):
                continue
            if not projects and row.get("company") and normalize_company_name(row["company"]) not in names:
                continue
            scoped.append((order, row))
        if not scoped:
            return None
        return max(scoped, key=lambda item: evaluation_recency(item[1], item[0]))[1]


@dataclass
class EvaluationSelection:
    index: EvaluationIndex
    baseline: Path | None
    overlays: list[Path]
    skipped: list[dict[str, str]]
    counts: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "baseline": str(self.baseline) if self.baseline else None,
            "overlays": [str(path) for path in self.overlays],
            "skipped": self.skipped,
            **self.counts,
        }


def _read_header(path: Path) -> dict[str, Any]:
    """Decode top-level metadata before results without reading large JD arrays."""
    with path.open(encoding="utf-8-sig") as stream:
        text = stream.read(MAX_HEADER_CHARS)
    decoder = json.JSONDecoder()
    position = 0

    def skip_space() -> None:
        nonlocal position
        while position < len(text) and text[position].isspace():
            position += 1

    skip_space()
    if text[position:position + 1] != "{":
        raise ValueError("evaluation must be a JSON object")
    position += 1
    header = {}
    while position < len(text):
        skip_space()
        if text[position:position + 1] == "}":
            return header
        key, position = decoder.raw_decode(text, position)
        if not isinstance(key, str):
            raise ValueError("invalid evaluation metadata key")
        skip_space()
        if text[position:position + 1] != ":":
            raise ValueError("invalid evaluation metadata separator")
        position += 1
        if key == "results":
            return header
        skip_space()
        header[key], position = decoder.raw_decode(text, position)
        skip_space()
        if text[position:position + 1] == ",":
            position += 1
        elif text[position:position + 1] != "}":
            raise ValueError("invalid or oversized evaluation metadata")
    raise ValueError("evaluation metadata exceeds header limit")


def _read_evaluation(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_EVALUATION_BYTES:
        raise ValueError("evaluation exceeds per-file byte limit")
    with path.open("rb") as stream:
        raw = stream.read(MAX_EVALUATION_BYTES + 1)
    if len(raw) > MAX_EVALUATION_BYTES:
        raise ValueError("evaluation exceeds per-file byte limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("evaluation must contain a results list")
    results = payload["results"]
    if len(results) > MAX_EVALUATION_RESULTS:
        raise ValueError("evaluation exceeds result limit")
    if any(
        not isinstance(row, dict)
        or not row.get("integration_status")
        or not (row.get("lead_key") or evaluation_urls(row))
        for row in results
    ):
        raise ValueError("invalid evaluation result")
    # Job/JD evidence is irrelevant to catalog selection and is not retained.
    payload["results"] = [
        {key: value for key, value in row.items() if key in _RESULT_FIELDS}
        for row in results
    ]
    return payload


def _compatible(
    payload: Mapping[str, Any], source: Any, *, explicit: bool, snapshot_sha256: str | None,
) -> bool:
    captured_at = payload.get("snapshot_captured_at")
    fingerprint = payload.get("snapshot_sha256")
    if fingerprint and (not snapshot_sha256 or fingerprint != snapshot_sha256):
        return False
    if not captured_at and not fingerprint:
        return explicit and not any(key.startswith("snapshot_") for key in payload)
    if captured_at:
        expected = evaluation_recency({"completed_at": source.captured_at})
        actual = evaluation_recency({"completed_at": captured_at})
        if not expected[0] or expected != actual:
            return False
    for field, value in (
        ("snapshot_rows_seen", source.rows_seen),
        ("snapshot_pages_fetched", source.pages_fetched),
    ):
        if field in payload and payload[field] != value:
            return False
    return True


def load_catalog_evaluations(
    source: Any,
    evaluation_path: Path | None = None,
    *,
    evaluation_dir: Path | None = None,
    snapshot_path: Path | None = None,
) -> EvaluationSelection:
    """Select a compatible full baseline plus newer partial overlays.

    Full means evidence for every consolidated evaluator lead, not that every
    source succeeded. A filename or summary count alone never establishes scope.
    Explicit legacy results-only artifacts are supported without auto-discovery.
    """
    leads = list(source.leads)
    expected_leads = list(consolidate_source_leads(leads))
    skipped: list[dict[str, str]] = []
    snapshot_sha256 = None
    if snapshot_path is not None:
        with snapshot_path.open("rb") as stream:
            snapshot_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    if evaluation_path is not None:
        paths = [evaluation_path] if evaluation_path.is_file() else []
    elif evaluation_dir is not None and evaluation_dir.is_dir():
        paths = []
        for index, path in enumerate(evaluation_dir.iterdir()):
            if index >= MAX_DIRECTORY_ENTRIES:
                raise ValueError("evaluation directory exceeds entry limit; use --evaluation")
            if path.suffix.lower() == ".json" and path.is_file():
                paths.append(path)
                if len(paths) > MAX_EVALUATION_FILES:
                    raise ValueError("evaluation directory exceeds file limit; use --evaluation")
        paths.sort()
    else:
        paths = []

    candidates = []
    for path in paths:
        try:
            header = _read_header(path)
            if not _compatible(
                header, source, explicit=evaluation_path is not None,
                snapshot_sha256=snapshot_sha256,
            ):
                raise ValueError("incompatible or missing snapshot identity")
            recency = evaluation_recency(header)
            if evaluation_path is None and not recency[0]:
                raise ValueError("missing valid evaluation completion time")
            candidates.append((recency, path))
        except (OSError, UnicodeError, ValueError) as exc:
            if evaluation_path is not None:
                raise ValueError(f"invalid explicit evaluation {path}: {exc}") from exc
            skipped.append({"path": str(path), "reason": str(exc)})

    candidates.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
    baseline = None
    partials = []
    total_bytes = 0
    loaded_files = 0
    for recency, path in candidates:
        total_bytes += min(path.stat().st_size, MAX_EVALUATION_BYTES + 1)
        if total_bytes > MAX_TOTAL_EVALUATION_BYTES:
            raise ValueError("evaluation loading exceeds total byte limit; use --evaluation")
        try:
            payload = _read_evaluation(path)
            loaded_files += 1
            if not _compatible(
                payload, source, explicit=evaluation_path is not None,
                snapshot_sha256=snapshot_sha256,
            ):
                raise ValueError("incompatible or missing snapshot identity")
            rows = payload["results"]
            recency = evaluation_recency(payload)
            if not recency[0]:
                recency = max((evaluation_recency(row) for row in rows), default=(0, 0.0, 0))
            if evaluation_path is None and not recency[0]:
                raise ValueError("missing valid evaluation completion time")
            for row in rows:
                if not evaluation_recency(row)[0] and recency[0]:
                    row["completed_at"] = datetime.fromtimestamp(recency[1], timezone.utc).isoformat()
                row["evaluation_file"] = str(path)
            indexed = EvaluationIndex(rows, leads)
            scope = payload.get("evaluation_scope") or payload.get("scope")
            full = bool(expected_leads) and scope not in {"partial", "targeted"} and all(
                any(indexed.for_source(lead, url) is not None for url in lead.source_urls or ("",))
                for lead in expected_leads
            )
            relevant = any(
                indexed.for_source(lead, url) is not None
                for lead in leads for url in lead.source_urls or ("",)
            )
            if relevant:
                artifact = (recency, path, rows, payload.get("baseline") or payload.get("baseline_path"))
                if full or evaluation_path is not None:
                    baseline = artifact
                    break
                partials.append(artifact)
            else:
                skipped.append({"path": str(path), "reason": "no matching snapshot leads"})
        except (OSError, UnicodeError, ValueError) as exc:
            if evaluation_path is not None:
                raise ValueError(f"invalid explicit evaluation {path}: {exc}") from exc
            skipped.append({"path": str(path), "reason": str(exc)})

    overlays = []
    for item in sorted(partials, key=lambda item: (item[0], str(item[1]))):
        if not baseline or item[0] <= baseline[0]:
            continue
        reference = item[3]
        if reference:
            reference_path = Path(reference)
            if not reference_path.is_absolute():
                reference_path = item[1].parent / reference_path
            if reference_path.resolve() != baseline[1].resolve():
                skipped.append({"path": str(item[1]), "reason": "overlay references a different baseline"})
                continue
        overlays.append(item)
    selected = ([baseline] if baseline else []) + overlays
    rows = [row for item in selected for row in item[2]]
    return EvaluationSelection(
        EvaluationIndex(rows, leads), baseline[1] if baseline else None,
        [item[1] for item in overlays], skipped,
        {
            "files_considered": len(paths),
            "files_loaded": loaded_files,
            "bytes_loaded": total_bytes,
            "expected_leads": len(expected_leads),
            "baseline_results": len(baseline[2]) if baseline else 0,
            "overlay_results": sum(len(item[2]) for item in overlays),
            "selected_results": len(rows),
        },
    )
