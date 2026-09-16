from __future__ import annotations

from pathlib import Path
from threading import RLock

from .context import build_scoring_context
from .loader import load_candidate_profile
from .models import CandidateProfile, CandidateScoringContext


class CandidateProfileProvider:
    """Reload the local profile only when its source file changes."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._lock = RLock()
        self._signature: tuple[int, int] | None = None
        self._profile: CandidateProfile | None = None

    def get(self) -> CandidateProfile:
        try:
            stat = self.path.stat()
        except OSError:
            return load_candidate_profile(self.path)
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._profile is None or self._signature != signature:
                self._profile = load_candidate_profile(self.path)
                self._signature = signature
            return self._profile

    def scoring_context(self) -> CandidateScoringContext:
        return build_scoring_context(self.get())


__all__ = ["CandidateProfileProvider"]
