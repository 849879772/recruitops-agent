from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Protocol

from .models import (
    ApprovalDecision,
    ApprovalPreview,
    ApprovalStatus,
    ApprovalToken,
    PolicyErrorCode,
)
from .policy import (
    approve_token,
    begin_token,
    complete_token,
    consume_token,
    issue_approval_token,
    reject_token,
    release_token,
)


class ApprovalPersistence(Protocol):
    def load(self) -> list[tuple[ApprovalToken, ApprovalPreview]]: ...

    def save(self, token: ApprovalToken, preview: ApprovalPreview) -> None: ...

    def get(self, token_id: str) -> tuple[ApprovalToken, ApprovalPreview] | None: ...

    def compare_and_set(
        self,
        expected: ApprovalToken,
        replacement: ApprovalToken,
        preview: ApprovalPreview,
    ) -> bool: ...

    def create_or_get(
        self,
        token: ApprovalToken,
        preview: ApprovalPreview,
    ) -> tuple[bool, tuple[ApprovalToken, ApprovalPreview] | None]: ...


class ApprovalRegistry:
    """Local approval center registry; it never executes the requested write."""

    def __init__(self, persistence: ApprovalPersistence | None = None) -> None:
        self._previews: dict[str, ApprovalPreview] = {}
        self._tokens: dict[str, ApprovalToken] = {}
        self._persistence = persistence
        self._loaded = persistence is None
        self._lock = RLock()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            assert self._persistence is not None
            for token, preview in self._persistence.load():
                self._tokens[token.token_id] = token
                self._previews[token.token_id] = preview
            self._loaded = True

    def _supports_atomic_persistence(self) -> bool:
        return self._persistence is not None and all(
            callable(getattr(self._persistence, name, None))
            for name in ("get", "compare_and_set")
        )

    def _supports_atomic_issue(self) -> bool:
        return self._supports_atomic_persistence() and callable(
            getattr(self._persistence, "create_or_get", None)
        )

    def _refresh_all(self) -> None:
        if not self._supports_atomic_persistence():
            return
        assert self._persistence is not None
        self._tokens.clear()
        self._previews.clear()
        for token, preview in self._persistence.load():
            self._tokens[token.token_id] = token
            self._previews[token.token_id] = preview

    def _current(self, token_id: str) -> tuple[ApprovalToken, ApprovalPreview]:
        if self._supports_atomic_persistence():
            assert self._persistence is not None
            state = self._persistence.get(token_id)
            if state is None:
                raise KeyError(f"approval token {token_id!r} was not found")
            token, preview = state
            self._tokens[token_id] = token
            self._previews[token_id] = preview
            return token, preview
        token = self._require(token_id)
        return token, self._previews[token_id]

    def _save(self, token: ApprovalToken) -> None:
        if self._persistence is not None:
            self._persistence.save(token, self._previews[token.token_id])

    @staticmethod
    def _same_preview_binding(left: ApprovalPreview, right: ApprovalPreview) -> bool:
        fields = (
            "task_id",
            "operation",
            "idempotency_key",
            "evidence_summary",
            "target_id",
            "payload",
            "before",
            "after",
            "cohort",
            "cohort_status",
            "jd_raw",
            "current_stage",
            "target_stage",
        )
        return all(getattr(left, field) == getattr(right, field) for field in fields)

    @staticmethod
    def _existing_issue_decision(token: ApprovalToken) -> ApprovalDecision:
        if token.status is ApprovalStatus.EXECUTING:
            return ApprovalDecision(
                allowed=False,
                status=token.status,
                reason="The existing approval is currently executing.",
                error_code=PolicyErrorCode.TOKEN_IN_PROGRESS,
                token=token,
            )
        if token.status is ApprovalStatus.REJECTED:
            return ApprovalDecision(
                allowed=False,
                status=token.status,
                reason="The existing approval was rejected.",
                error_code=PolicyErrorCode.TOKEN_REJECTED,
                token=token,
            )
        if token.status is ApprovalStatus.EXPIRED:
            return ApprovalDecision(
                allowed=False,
                status=token.status,
                reason="The existing approval expired.",
                error_code=PolicyErrorCode.TOKEN_EXPIRED,
                token=token,
            )
        return ApprovalDecision(
            allowed=True,
            status=token.status,
            reason="The existing approval capability was reused idempotently.",
            token=token,
        )

    @staticmethod
    def _binding_conflict(token: ApprovalToken) -> ApprovalDecision:
        return ApprovalDecision(
            allowed=False,
            status=token.status,
            reason="The idempotency key is already bound to a different approval preview.",
            error_code=PolicyErrorCode.TOKEN_STATE_CONFLICT,
            token=token,
        )

    def issue(self, preview: ApprovalPreview, *, now: datetime | None = None) -> ApprovalDecision:
        self._ensure_loaded()
        with self._lock:
            if self._supports_atomic_persistence():
                self._refresh_all()
                existing = next(
                    (
                        (token, self._previews[token.token_id])
                        for token in self._tokens.values()
                        if token.idempotency_key == preview.idempotency_key
                    ),
                    None,
                )
                if existing is not None:
                    existing_token, existing_preview = existing
                    if not self._same_preview_binding(existing_preview, preview):
                        return self._binding_conflict(existing_token)
                    return self._existing_issue_decision(existing_token)
                existing_idempotency_keys = () if self._supports_atomic_issue() else {
                    token.idempotency_key for token in self._tokens.values()
                }
            else:
                existing_idempotency_keys = ()
            decision = issue_approval_token(
                preview,
                now=now,
                existing_idempotency_keys=existing_idempotency_keys,
            )
            if decision.token is not None:
                if self._supports_atomic_issue():
                    assert self._persistence is not None
                    _created, state = self._persistence.create_or_get(decision.token, preview)
                    if state is None:
                        return ApprovalDecision(
                            allowed=False,
                            status=decision.status,
                            reason="The idempotency key could not be bound to a durable approval.",
                            error_code=PolicyErrorCode.TOKEN_STATE_CONFLICT,
                        )
                    existing_token, existing_preview = state
                    self._tokens[existing_token.token_id] = existing_token
                    self._previews[existing_token.token_id] = existing_preview
                    if not self._same_preview_binding(existing_preview, preview):
                        return self._binding_conflict(existing_token)
                    if _created:
                        return decision.model_copy(update={"token": existing_token})
                    return self._existing_issue_decision(existing_token)
                self._previews[decision.token.token_id] = preview
                self._tokens[decision.token.token_id] = decision.token
                self._save(decision.token)
            return decision

    def approve(self, token_id: str, *, now: datetime | None = None) -> ApprovalDecision:
        return self._transition(
            token_id,
            "approve",
            lambda token, _preview: approve_token(token, now=now),
            now=now,
        )

    def reject(
        self,
        token_id: str,
        *,
        now: datetime | None = None,
        reason: str = "The operator rejected this write preview.",
    ) -> ApprovalDecision:
        return self._transition(
            token_id,
            "reject",
            lambda token, _preview: reject_token(token, now=now, reason=reason),
            now=now,
        )

    def list(self) -> list[ApprovalToken]:
        self._ensure_loaded()
        with self._lock:
            self._refresh_all()
            return list(self._tokens.values())

    def queue(self) -> list[tuple[ApprovalToken, ApprovalPreview]]:
        """Return each capability together with the exact preview it authorizes."""

        self._ensure_loaded()
        with self._lock:
            self._refresh_all()
            return [
                (token, self._previews[token.token_id])
                for token in self._tokens.values()
            ]

    def preview(self, token_id: str) -> ApprovalPreview:
        self._ensure_loaded()
        with self._lock:
            _token, preview = self._current(token_id)
            return preview

    def token(self, token_id: str) -> ApprovalToken:
        self._ensure_loaded()
        with self._lock:
            token, _preview = self._current(token_id)
            return token

    def consume(
        self,
        token_id: str,
        *,
        now: datetime | None = None,
        existing_idempotency_keys: set[str] | frozenset[str] = frozenset(),
    ) -> ApprovalDecision:
        return self._transition(
            token_id,
            "consume",
            lambda token, preview: consume_token(
                token,
                preview,
                now=now,
                existing_idempotency_keys=existing_idempotency_keys,
            ),
            now=now,
        )

    def begin(
        self,
        token_id: str,
        *,
        now: datetime | None = None,
        existing_idempotency_keys: set[str] | frozenset[str] = frozenset(),
    ) -> ApprovalDecision:
        """Claim one approved write with the persistence CAS before side effects."""

        return self._transition(
            token_id,
            "begin",
            lambda token, preview: begin_token(
                token,
                preview,
                now=now,
                existing_idempotency_keys=existing_idempotency_keys,
            ),
            now=now,
        )

    def complete(
        self,
        token_id: str,
        *,
        now: datetime | None = None,
    ) -> ApprovalDecision:
        """Commit a claimed write after its adapter reports success."""

        return self._transition(
            token_id,
            "complete",
            lambda token, _preview: complete_token(token, now=now),
            now=now,
        )

    def release(
        self,
        token_id: str,
        *,
        now: datetime | None = None,
    ) -> ApprovalDecision:
        """Release a failed, uncommitted execution claim for a safe retry."""

        return self._transition(
            token_id,
            "release",
            lambda token, _preview: release_token(token, now=now),
            now=now,
        )

    def _transition(
        self,
        token_id: str,
        operation: str,
        decide,
        *,
        now: datetime | None,
    ) -> ApprovalDecision:
        self._ensure_loaded()
        with self._lock:
            token, preview = self._current(token_id)
            decision = decide(token, preview)
            if not self._supports_atomic_persistence():
                if decision.token is not None:
                    self._tokens[token_id] = decision.token
                    self._save(decision.token)
                return decision

            if decision.token is None:
                return decision
            assert self._persistence is not None
            if self._persistence.compare_and_set(token, decision.token, preview):
                self._tokens[token_id] = decision.token
                return decision

            latest, _latest_preview = self._current(token_id)
            return self._conflict_decision(operation, latest, now=now)

    @staticmethod
    def _conflict_decision(
        operation: str,
        token: ApprovalToken,
        *,
        now: datetime | None,
    ) -> ApprovalDecision:
        if token.consumed or token.status is ApprovalStatus.CONSUMED:
            return ApprovalDecision(
                allowed=False,
                status=ApprovalStatus.CONSUMED,
                reason="The approval token has already been consumed by another worker.",
                error_code=PolicyErrorCode.TOKEN_ALREADY_CONSUMED,
                token=token,
            )
        if token.status is ApprovalStatus.EXECUTING:
            return ApprovalDecision(
                allowed=False,
                status=ApprovalStatus.EXECUTING,
                reason="The approval token is currently executing a write.",
                error_code=PolicyErrorCode.TOKEN_IN_PROGRESS,
                token=token,
            )
        if token.status is ApprovalStatus.REJECTED:
            return ApprovalDecision(
                allowed=False,
                status=ApprovalStatus.REJECTED,
                reason="The approval token was rejected by another worker.",
                error_code=PolicyErrorCode.TOKEN_REJECTED,
                token=token,
            )
        if token.status is ApprovalStatus.EXPIRED:
            return ApprovalDecision(
                allowed=False,
                status=ApprovalStatus.EXPIRED,
                reason="The approval token expired before this worker could update it.",
                error_code=PolicyErrorCode.TOKEN_EXPIRED,
                token=token,
            )
        if operation == "approve" and token.status is ApprovalStatus.APPROVED:
            return approve_token(token, now=now)
        return ApprovalDecision(
            allowed=False,
            status=token.status,
            reason="The approval token changed before this worker could update it.",
            error_code=PolicyErrorCode.TOKEN_STATE_CONFLICT,
            token=token,
        )

    def _require(self, token_id: str) -> ApprovalToken:
        try:
            return self._tokens[token_id]
        except KeyError as exc:
            raise KeyError(f"approval token {token_id!r} was not found") from exc


__all__ = ["ApprovalPersistence", "ApprovalRegistry"]
