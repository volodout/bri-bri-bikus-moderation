"""Business logic for moderation decisions."""

from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any

from moderation.b2b_client import B2BClientError
from moderation.database import ModerationStore
from moderation.errors import ConflictError, ForbiddenError, NotFoundError, UpstreamError, ValidationError
from moderation.product_events import utc_now


@dataclass(frozen=True)
class DecisionResult:
    product_id: str
    status: str

    def as_json(self) -> dict[str, str]:
        return {"product_id": self.product_id, "status": self.status}


class DecisionService:
    def __init__(self, store: ModerationStore, b2b_client: Any):
        self.store = store
        self.b2b_client = b2b_client

    def approve(self, product_id: str, moderator_id: str, payload: Any | None = None) -> DecisionResult:
        product_id = _validate_uuid(product_id, "product_id")
        moderator_id = _validate_uuid(moderator_id, "X-Moderator-Id")
        moderator_comment = _optional_comment(payload)
        self.store.ensure_schema()

        product = self._fetch_product(product_id)
        skus = product.get("skus")
        if not isinstance(skus, list) or len(skus) == 0:
            raise ConflictError("Product has no SKUs, cannot approve")

        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM product_moderation
                WHERE product_id = ?
                """,
                (product_id,),
            ).fetchone()
            _ensure_assigned_for_decision(row, moderator_id)

            now = utc_now()
            connection.execute(
                """
                UPDATE product_moderation
                SET status = 'MODERATED',
                    date_moderation = ?,
                    moderator_comment = ?,
                    blocking_reason_id = NULL
                WHERE product_id = ?
                  AND status = 'IN_REVIEW'
                  AND moderator_id = ?
                """,
                (now, moderator_comment, product_id, moderator_id),
            )
            if connection.total_changes == 0:
                raise ConflictError("Product was changed during review")
            connection.execute(
                """
                DELETE FROM product_moderation_field_report
                WHERE product_moderation_id = ?
                """,
                (row["id"],),
            )
            self._send_moderated_event(product_id)

        return DecisionResult(product_id=product_id, status="MODERATED")

    def _fetch_product(self, product_id: str) -> dict[str, Any]:
        try:
            return self.b2b_client.fetch_product(product_id)
        except B2BClientError as error:
            raise UpstreamError(str(error)) from error

    def _send_moderated_event(self, product_id: str) -> None:
        try:
            self.b2b_client.send_moderation_event(
                {
                    "idempotency_key": str(uuid.uuid4()),
                    "product_id": product_id,
                    "status": "MODERATED",
                }
            )
        except B2BClientError as error:
            raise UpstreamError(str(error)) from error


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ValidationError(f"{field_name} must be a UUID") from error


def _optional_comment(payload: Any | None) -> str | None:
    if payload in (None, b""):
        return None
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")
    comment = payload.get("moderator_comment")
    if comment is None:
        return None
    if not isinstance(comment, str):
        raise ValidationError("moderator_comment must be a string")
    return comment


def _ensure_assigned_for_decision(row: Any, moderator_id: str) -> None:
    if row is None:
        raise NotFoundError("Product not found in moderation queue")
    if row["status"] == "HARD_BLOCKED":
        raise ConflictError("Product is permanently blocked")
    if row["status"] != "IN_REVIEW":
        raise ConflictError("Product is not in review status")
    if row["moderator_id"] != moderator_id:
        raise ForbiddenError("This moderation card is not assigned to you")

