"""Configuration records used by the standalone recruitment crawler core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = (value,)
    else:
        try:
            values = tuple(value)  # type: ignore[arg-type]
        except TypeError:
            values = (value,)
    return tuple(str(item).strip() for item in values if str(item).strip())


@dataclass(frozen=True, slots=True)
class CompanyConfig:
    """One configured company, compatible with the legacy ``company`` dict."""

    name: str
    careers_url: str
    crawler: str
    campaign_url: str = ""
    campaign_urls: tuple[str, ...] = ()
    link_kind: str = ""
    campaign_text: str = ""
    aliases: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in ("name", "careers_url", "crawler", "campaign_url", "link_kind", "campaign_text"):
            object.__setattr__(self, field_name, str(getattr(self, field_name) or "").strip())
        object.__setattr__(self, "campaign_urls", _string_tuple(self.campaign_urls))
        object.__setattr__(self, "aliases", _string_tuple(self.aliases))
        object.__setattr__(self, "extra", dict(self.extra or {}))

    @classmethod
    def from_legacy(cls, company: CompanyConfig | Mapping[str, Any]) -> CompanyConfig:
        """Normalize a model or the old project-shaped company mapping."""
        if isinstance(company, cls):
            return company
        if not isinstance(company, Mapping):
            raise TypeError("company must be a CompanyConfig or mapping")

        raw = dict(company)
        known = {
            "name", "company", "careers_url", "url", "crawler", "campaign_url",
            "campaign_urls", "link_kind", "campaign_text", "aliases",
        }
        return cls(
            name=str(raw.get("name") or raw.get("company") or ""),
            careers_url=str(raw.get("careers_url") or raw.get("url") or ""),
            crawler=str(raw.get("crawler") or ""),
            campaign_url=str(raw.get("campaign_url") or ""),
            campaign_urls=raw.get("campaign_urls") or (),
            link_kind=str(raw.get("link_kind") or ""),
            campaign_text=str(raw.get("campaign_text") or ""),
            aliases=raw.get("aliases") or (),
            extra={key: value for key, value in raw.items() if key not in known},
        )

    from_dict = from_legacy

    def to_dict(self) -> dict[str, Any]:
        """Return a legacy-shaped mutable mapping for downstream adapters."""
        result = dict(self.extra)
        result.update(
            {
                "name": self.name,
                "careers_url": self.careers_url,
                "crawler": self.crawler,
                "campaign_url": self.campaign_url,
                "campaign_urls": list(self.campaign_urls),
                "link_kind": self.link_kind,
                "campaign_text": self.campaign_text,
                "aliases": list(self.aliases),
            }
        )
        return result

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


CompanyRecord = CompanyConfig


__all__ = ["CompanyConfig", "CompanyRecord"]
