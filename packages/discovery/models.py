"""Typed, side-effect-free models for source discovery."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal
import unicodedata


ReconciliationStatus = Literal["existing", "new", "ambiguous"]


def compact_text(value: object) -> str:
    """Normalize Unicode and collapse ordinary display whitespace."""

    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).strip()


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    values: Iterable[object]
    if isinstance(value, str):
        values = (value,)
    else:
        try:
            values = tuple(value)  # type: ignore[arg-type]
        except TypeError:
            values = (value,)

    result: list[str] = []
    for item in values:
        text = compact_text(item)
        if text and text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SourceLead:
    """One company lead emitted by a discovery source.

    ``canonical_name`` is the source-side display name after source-specific
    aliases have been applied.  Company reconciliation still performs its own
    exact normalization against configured names and aliases.
    """

    canonical_name: str
    source: str
    source_name: str = ""
    source_urls: tuple[str, ...] = ()
    source_identity: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    matched_company: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        canonical_name = compact_text(self.canonical_name)
        source = compact_text(self.source)
        source_name = compact_text(self.source_name) or canonical_name
        if not canonical_name:
            raise ValueError("SourceLead.canonical_name cannot be empty")
        if not source:
            raise ValueError("SourceLead.source cannot be empty")
        object.__setattr__(self, "canonical_name", canonical_name)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_name", source_name)
        object.__setattr__(self, "source_urls", _string_tuple(self.source_urls))
        identity = compact_text(self.source_identity) or None
        object.__setattr__(self, "source_identity", identity)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        matched = compact_text(self.matched_company) or None
        object.__setattr__(self, "matched_company", matched)

    @property
    def company_name(self) -> str:
        """Compatibility-friendly name for callers that do not need the qualifier."""

        return self.canonical_name

    @property
    def name(self) -> str:
        return self.canonical_name

    @property
    def links(self) -> tuple[str, ...]:
        return self.source_urls

    @property
    def source_url(self) -> str:
        return self.source_urls[0] if self.source_urls else ""

    def with_match(self, company_name: str | None) -> SourceLead:
        return replace(self, matched_company=company_name)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "canonical_name": self.canonical_name,
            "company_name": self.canonical_name,
            "source": self.source,
            "source_name": self.source_name,
            "source_urls": list(self.source_urls),
            "links": list(self.source_urls),
            "source_identity": self.source_identity,
            "metadata": dict(self.metadata),
        }
        if self.matched_company is not None:
            result["matched_company"] = self.matched_company
        return result


@dataclass(frozen=True, slots=True)
class SourceSyncResult:
    """A read result from one source; it carries no write capability."""

    source: str
    source_url: str = ""
    leads: tuple[SourceLead, ...] = ()
    rows_seen: int = 0
    accepted_rows: int | None = None
    pages_fetched: int = 0
    captured_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        source = compact_text(self.source)
        if not source:
            raise ValueError("SourceSyncResult.source cannot be empty")
        if self.rows_seen < 0 or self.pages_fetched < 0:
            raise ValueError("SourceSyncResult counters cannot be negative")
        if self.accepted_rows is not None and self.accepted_rows < 0:
            raise ValueError("SourceSyncResult.accepted_rows cannot be negative")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_url", compact_text(self.source_url))
        object.__setattr__(self, "leads", tuple(self.leads))
        object.__setattr__(self, "captured_at", compact_text(self.captured_at) or None)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def companies(self) -> tuple[SourceLead, ...]:
        return self.leads

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_url": self.source_url,
            "leads": [lead.to_dict() for lead in self.leads],
            "rows_seen": self.rows_seen,
            "accepted_rows": self.accepted_rows,
            "pages_fetched": self.pages_fetched,
            "captured_at": self.captured_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompanyReconciliationResult:
    """Deterministic reconciliation buckets for discovered company leads."""

    existing: tuple[SourceLead, ...] = ()
    new: tuple[SourceLead, ...] = ()
    ambiguous: tuple[SourceLead, ...] = ()
    matched_companies: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    ambiguous_reasons: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "existing", tuple(self.existing))
        object.__setattr__(self, "new", tuple(self.new))
        object.__setattr__(self, "ambiguous", tuple(self.ambiguous))
        object.__setattr__(self, "matched_companies", dict(self.matched_companies or {}))
        object.__setattr__(
            self,
            "ambiguous_reasons",
            {
                str(key): tuple(str(reason) for reason in reasons)
                for key, reasons in dict(self.ambiguous_reasons or {}).items()
            },
        )

    @property
    def counts(self) -> dict[ReconciliationStatus, int]:
        return {
            "existing": len(self.existing),
            "new": len(self.new),
            "ambiguous": len(self.ambiguous),
        }

    @property
    def existing_companies(self) -> tuple[SourceLead, ...]:
        return self.existing

    @property
    def new_companies(self) -> tuple[SourceLead, ...]:
        return self.new

    @property
    def ambiguous_companies(self) -> tuple[SourceLead, ...]:
        return self.ambiguous

    def to_dict(self) -> dict[str, Any]:
        return {
            "existing": [lead.to_dict() for lead in self.existing],
            "new": [lead.to_dict() for lead in self.new],
            "ambiguous": [lead.to_dict() for lead in self.ambiguous],
            "counts": self.counts,
            "matched_companies": dict(self.matched_companies),
            "ambiguous_reasons": {
                key: list(reasons) for key, reasons in self.ambiguous_reasons.items()
            },
        }


__all__ = [
    "CompanyReconciliationResult",
    "ReconciliationStatus",
    "SourceLead",
    "SourceSyncResult",
    "compact_text",
]
