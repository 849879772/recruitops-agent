from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import Storage
from .models import BrowserReviewReceipt


class BrowserReviewReceiptStore:
    """Persist the authorization and observation phases of browser reviews."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._initialized = False

    def get(
        self,
        review_id: str,
        target_id: str,
        action_attempt_id: str,
    ) -> BrowserReviewReceipt | None:
        self._initialize()
        with self.storage.session() as session:
            return self._get_in_session(session, review_id, target_id, action_attempt_id)

    def list_for_review(self, review_id: str) -> list[BrowserReviewReceipt]:
        """Return durable browser attempts for one review in creation order."""

        self._initialize()
        with self.storage.session() as session:
            return list(
                session.scalars(
                    select(BrowserReviewReceipt)
                    .where(BrowserReviewReceipt.review_id == review_id)
                    .order_by(BrowserReviewReceipt.created_at)
                )
            )

    def record_authorized_attempt(
        self,
        review_id: str,
        target_id: str,
        action_attempt_id: str,
        normalized_url: str,
        application_ids: Sequence[str],
    ) -> BrowserReviewReceipt:
        """Create an authorization receipt, preserving an already observed receipt."""

        self._initialize()
        application_ids_value = list(application_ids)
        with self.storage.write_transaction() as session:
            receipt = self._get_in_session(session, review_id, target_id, action_attempt_id)
            if receipt is None:
                receipt = BrowserReviewReceipt(
                    review_id=review_id,
                    target_id=target_id,
                    action_attempt_id=action_attempt_id,
                    normalized_url=normalized_url,
                    application_ids=application_ids_value,
                    status="authorized",
                )
                session.add(receipt)
                session.flush()
                return receipt

            if not self._authorization_matches(receipt, normalized_url, application_ids_value):
                raise ValueError("browser review authorization conflicts with existing receipt")
            if receipt.status not in {"authorized", "observed", "completed"}:
                raise ValueError(f"unsupported browser review receipt status: {receipt.status}")
            return receipt

    def save_observation(
        self,
        review_id: str,
        target_id: str,
        action_attempt_id: str,
        normalized_url: str,
        application_ids: Sequence[str],
        entries: list[dict[str, Any]],
        captured_at: datetime,
        observation_id: str,
    ) -> BrowserReviewReceipt:
        """Bind an observation to a matching, previously authorized attempt."""

        self._initialize()
        application_ids_value = list(application_ids)
        entries_value = deepcopy(entries)
        with self.storage.write_transaction() as session:
            receipt = self._get_in_session(session, review_id, target_id, action_attempt_id)
            if receipt is None:
                raise ValueError("browser review observation has no authorized attempt")
            if not self._authorization_matches(receipt, normalized_url, application_ids_value):
                raise ValueError("browser review observation does not match authorization")

            if receipt.status in {"observed", "completed"}:
                if self._observation_matches(
                    receipt,
                    observation_id=observation_id,
                    entries=entries_value,
                    captured_at=captured_at,
                ):
                    return receipt
                raise ValueError("browser review observation conflicts with existing receipt")
            if receipt.status != "authorized":
                raise ValueError(f"unsupported browser review receipt status: {receipt.status}")

            receipt.observation_id = observation_id
            receipt.entries = entries_value
            receipt.captured_at = captured_at
            receipt.status = "observed"
            receipt.error_code = None
            session.flush()
            return receipt

    def save_result(
        self,
        review_id: str,
        target_id: str,
        action_attempt_id: str,
        result: dict[str, Any],
    ) -> BrowserReviewReceipt:
        """Attach the stable API result to an observed receipt exactly once."""

        self._initialize()
        result_value = deepcopy(result)
        with self.storage.write_transaction() as session:
            receipt = self._get_in_session(session, review_id, target_id, action_attempt_id)
            if receipt is None:
                raise ValueError("browser review result has no observation receipt")
            if receipt.status == "completed":
                if receipt.result == result_value:
                    return receipt
                raise ValueError("browser review result conflicts with existing receipt")
            if receipt.status != "observed":
                raise ValueError("browser review result requires an observed receipt")
            receipt.result = result_value
            receipt.status = "completed"
            session.flush()
            return receipt

    def save_error(
        self,
        review_id: str,
        target_id: str,
        action_attempt_id: str,
        error_code: str,
    ) -> BrowserReviewReceipt:
        """Finish an authorized browser attempt that needs user intervention."""

        self._initialize()
        with self.storage.write_transaction() as session:
            receipt = self._get_in_session(session, review_id, target_id, action_attempt_id)
            if receipt is None:
                raise ValueError("browser review error has no authorized attempt")
            result = {"status": "needs_attention", "error_code": error_code}
            if receipt.status == "completed":
                if receipt.error_code == error_code and receipt.result == result:
                    return receipt
                raise ValueError("browser review error conflicts with completed receipt")
            if receipt.status != "authorized":
                raise ValueError("browser review error requires an authorized attempt")
            receipt.error_code = error_code
            receipt.result = result
            receipt.status = "completed"
            session.flush()
            return receipt

    def save_or_get(
        self,
        review_id: BrowserReviewReceipt | str | None = None,
        target_id: str | None = None,
        action_attempt_id: str | None = None,
        normalized_url: str | None = None,
        application_ids: Sequence[str] | None = None,
        entries: list[dict[str, Any]] | None = None,
        captured_at: datetime | None = None,
        status: str | None = None,
        observation_id: str | None = None,
        error_code: str | None = None,
        *,
        receipt: BrowserReviewReceipt | None = None,
    ) -> BrowserReviewReceipt:
        """Return an equal receipt or persist one, rejecting same-key conflicts."""

        candidate = receipt
        if isinstance(review_id, BrowserReviewReceipt):
            if candidate is not None:
                raise TypeError("receipt was provided twice")
            if any(
                value is not None
                for value in (
                    target_id,
                    action_attempt_id,
                    normalized_url,
                    application_ids,
                    entries,
                    captured_at,
                    observation_id,
                    error_code,
                )
            ) or (status is not None and status != review_id.status):
                raise TypeError("receipt cannot be combined with receipt fields")
            candidate = review_id
        elif candidate is None:
            if any(
                value is None
                for value in (
                    review_id,
                    target_id,
                    action_attempt_id,
                    normalized_url,
                    application_ids,
                )
            ):
                raise TypeError("receipt identity and authorization fields are required")
            candidate = BrowserReviewReceipt(
                review_id=review_id,
                target_id=target_id,
                action_attempt_id=action_attempt_id,
                normalized_url=normalized_url,
                application_ids=list(application_ids),
                entries=deepcopy(entries),
                captured_at=captured_at,
                status=status or "authorized",
                observation_id=observation_id,
                error_code=error_code,
            )
        else:
            if review_id is not None or any(
                value is not None
                for value in (
                    target_id,
                    action_attempt_id,
                    normalized_url,
                    application_ids,
                    entries,
                    captured_at,
                    observation_id,
                    error_code,
                )
            ) or (status is not None and status != candidate.status):
                raise TypeError("receipt cannot be combined with receipt fields")

        assert candidate is not None
        values = self._content_values(candidate)
        self._initialize()
        with self.storage.write_transaction() as session:
            existing = self._get_in_session(
                session,
                values["review_id"],
                values["target_id"],
                values["action_attempt_id"],
            )
            if existing is not None:
                if self._content_matches(existing, values):
                    return existing
                raise ValueError("browser review receipt conflicts with existing receipt")

            saved = BrowserReviewReceipt(**values)
            session.add(saved)
            session.flush()
            return saved

    def _initialize(self) -> None:
        if not self._initialized:
            self.storage.initialize()
            self._initialized = True

    @staticmethod
    def _get_in_session(
        session: Session,
        review_id: str,
        target_id: str,
        action_attempt_id: str,
    ) -> BrowserReviewReceipt | None:
        return session.scalar(
            select(BrowserReviewReceipt).where(
                BrowserReviewReceipt.review_id == review_id,
                BrowserReviewReceipt.target_id == target_id,
                BrowserReviewReceipt.action_attempt_id == action_attempt_id,
            )
        )

    @staticmethod
    def _authorization_matches(
        receipt: BrowserReviewReceipt,
        normalized_url: str,
        application_ids: list[str],
    ) -> bool:
        return (
            receipt.normalized_url == normalized_url
            and receipt.application_ids == application_ids
        )

    @staticmethod
    def _observation_matches(
        receipt: BrowserReviewReceipt,
        *,
        observation_id: str,
        entries: list[dict[str, Any]],
        captured_at: datetime,
    ) -> bool:
        return (
            receipt.observation_id == observation_id
            and receipt.entries == entries
            and BrowserReviewReceiptStore._datetimes_equal(receipt.captured_at, captured_at)
        )

    @classmethod
    def _content_matches(
        cls,
        receipt: BrowserReviewReceipt,
        values: dict[str, Any],
    ) -> bool:
        return (
            receipt.normalized_url == values["normalized_url"]
            and receipt.application_ids == values["application_ids"]
            and receipt.entries == values["entries"]
            and cls._datetimes_equal(receipt.captured_at, values["captured_at"])
            and receipt.status == values["status"]
            and receipt.observation_id == values["observation_id"]
            and receipt.error_code == values["error_code"]
            and receipt.result == values["result"]
        )

    @staticmethod
    def _content_values(receipt: BrowserReviewReceipt) -> dict[str, Any]:
        return {
            "review_id": receipt.review_id,
            "target_id": receipt.target_id,
            "action_attempt_id": receipt.action_attempt_id,
            "normalized_url": receipt.normalized_url,
            "application_ids": deepcopy(receipt.application_ids),
            "entries": deepcopy(receipt.entries),
            "captured_at": receipt.captured_at,
            "status": receipt.status,
            "observation_id": receipt.observation_id,
            "error_code": receipt.error_code,
            "result": deepcopy(receipt.result),
        }

    @staticmethod
    def _datetimes_equal(left: datetime | None, right: datetime | None) -> bool:
        if left is None or right is None:
            return left is right
        left_utc = (
            left.astimezone(timezone.utc)
            if left.tzinfo is not None
            else left.replace(tzinfo=timezone.utc)
        )
        right_utc = (
            right.astimezone(timezone.utc)
            if right.tzinfo is not None
            else right.replace(tzinfo=timezone.utc)
        )
        return left_utc == right_utc


BrowserReviewStore = BrowserReviewReceiptStore


__all__ = ["BrowserReviewReceiptStore", "BrowserReviewStore"]
