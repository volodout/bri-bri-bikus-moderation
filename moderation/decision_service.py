"""Business logic for moderation decisions."""

from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any

from moderation.b2b_client import B2BClientError
from moderation.database import ModerationStore
from moderation.errors import (
    BusinessError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UpstreamError,
    ValidationError,
)
from moderation.product_events import utc_now

FIELD_REPORT_SEVERITIES = {"INFO", "WARNING", "ERROR"}


@dataclass(frozen=True)
class DecisionResult:
    product_id: str
    status: str
    product_moderation_id: str | None = None
    seller_id: str | None = None
    kind: str | None = None
    queue_priority: int | None = None
    created_at: str | None = None

    def as_json(self) -> dict[str, Any]:
        if (
            self.product_moderation_id is not None
            and self.seller_id is not None
            and self.kind is not None
            and self.queue_priority is not None
            and self.created_at is not None
        ):
            return {
                "id": self.product_moderation_id,
                "product_id": self.product_id,
                "seller_id": self.seller_id,
                "kind": self.kind,
                "status": self.status,
                "queue_priority": self.queue_priority,
                "created_at": self.created_at,
            }
        payload = {"product_id": self.product_id, "status": self.status}
        if self.product_moderation_id is not None:
            payload["product_moderation_id"] = self.product_moderation_id
        return payload


class DecisionService:
    def __init__(self, store: ModerationStore, b2b_client: Any):
        self.store = store
        self.b2b_client = b2b_client

    def approve_ticket(self, ticket_id: str, moderator_id: str, payload: Any | None = None) -> DecisionResult:
        ticket_id = _validate_uuid(ticket_id, "ticket_id")
        self.store.ensure_schema()
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT product_id
                FROM product_moderation
                WHERE id = ?
                """,
                (ticket_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("Ticket not found")
        return self._approve(row["product_id"], moderator_id, payload, product_moderation_id=ticket_id)

    def approve(self, product_id: str, moderator_id: str, payload: Any | None = None) -> DecisionResult:
        return self._approve(product_id, moderator_id, payload)

    def _approve(
        self,
        product_id: str,
        moderator_id: str,
        payload: Any | None = None,
        product_moderation_id: str | None = None,
    ) -> DecisionResult:
        product_id = _validate_uuid(product_id, "product_id")
        moderator_id = _validate_uuid(moderator_id, "X-Moderator-Id")
        moderator_comment = _optional_comment(payload)
        self.store.ensure_schema()

        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM product_moderation
                WHERE product_id = ?
                """,
                (product_id,),
            ).fetchone()
            _ensure_assigned_for_decision(row, moderator_id)

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
            cursor = connection.execute(
                """
                UPDATE product_moderation
                SET status = 'APPROVED',
                    date_moderation = ?,
                    moderator_comment = ?,
                    blocking_reason_id = NULL
                WHERE product_id = ?
                  AND status = 'IN_REVIEW'
                  AND moderator_id = ?
                """,
                (now, moderator_comment, product_id, moderator_id),
            )
            if cursor.rowcount == 0:
                raise ConflictError("Product was changed during review")
            connection.execute(
                """
                DELETE FROM product_moderation_field_report
                WHERE product_moderation_id = ?
                """,
                (row["id"],),
            )
            self._send_moderated_event(product_id, moderator_id, moderator_comment)

        return DecisionResult(
            product_id=product_id,
            status="APPROVED",
            product_moderation_id=product_moderation_id or row["id"],
            seller_id=row["seller_id"],
            kind=_ticket_kind(row["json_before"]),
            queue_priority=row["queue_priority"],
            created_at=row["date_created"],
        )

    def decline(self, product_id: str, moderator_id: str, payload: Any) -> DecisionResult:
        return self._decline(product_id, moderator_id, payload)

    def block_ticket(self, ticket_id: str, moderator_id: str, payload: Any) -> DecisionResult:
        ticket_id = _validate_uuid(ticket_id, "ticket_id")
        self.store.ensure_schema()
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT product_id
                FROM product_moderation
                WHERE id = ?
                """,
                (ticket_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("Ticket not found")
        return self._decline(row["product_id"], moderator_id, payload, product_moderation_id=ticket_id)

    def _decline(
        self,
        product_id: str,
        moderator_id: str,
        payload: Any,
        product_moderation_id: str | None = None,
    ) -> DecisionResult:
        product_id = _validate_uuid(product_id, "product_id")
        moderator_id = _validate_uuid(moderator_id, "X-Moderator-Id")
        request = _decline_request(payload)
        self.store.ensure_schema()

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

            reasons = _get_blocking_reasons(connection, request["blocking_reason_ids"])
            if any(reason is None for reason in reasons):
                raise BusinessError("Blocking reason not found")
            primary_reason = _primary_blocking_reason(reasons)
            hard_block = any(bool(reason["hard_block"]) for reason in reasons)
            decision_status = "HARD_BLOCKED" if hard_block else "BLOCKED"

            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE product_moderation
                SET status = ?,
                    date_moderation = ?,
                    blocking_reason_id = ?,
                    moderator_comment = ?
                WHERE product_id = ?
                  AND status = 'IN_REVIEW'
                  AND moderator_id = ?
                """,
                (
                    decision_status,
                    now,
                    primary_reason["id"],
                    request["moderator_comment"],
                    product_id,
                    moderator_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ConflictError("Product was changed during review")
            connection.execute(
                """
                DELETE FROM product_moderation_field_report
                WHERE product_moderation_id = ?
                """,
                (row["id"],),
            )
            for report in request["field_reports"]:
                connection.execute(
                    """
                    INSERT INTO product_moderation_field_report (
                        id,
                        product_moderation_id,
                        field_path,
                        sku_id,
                        message,
                        severity,
                        date_created
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        row["id"],
                        report["field_path"],
                        report["sku_id"],
                        report["message"],
                        report["severity"],
                        now,
                    ),
                )
            self._send_blocked_event(
                product_id,
                hard_block=hard_block,
                reason_id=primary_reason["id"],
                moderator_id=moderator_id,
                moderator_comment=request["moderator_comment"],
                field_reports=request["field_reports"],
            )

        return DecisionResult(
            product_id=product_id,
            status=decision_status,
            product_moderation_id=product_moderation_id,
        )

    def _fetch_product(self, product_id: str) -> dict[str, Any]:
        try:
            return self.b2b_client.fetch_product(product_id)
        except B2BClientError as error:
            raise UpstreamError(str(error)) from error

    def _send_moderated_event(
        self,
        product_id: str,
        moderator_id: str | None,
        moderator_comment: str | None,
    ) -> None:
        try:
            self.b2b_client.send_moderation_event(
                {
                    "idempotency_key": str(uuid.uuid4()),
                    "product_id": product_id,
                    "event_type": "MODERATED",
                    "moderator_id": moderator_id,
                    "moderator_comment": moderator_comment,
                    "occurred_at": utc_now(),
                }
            )
        except B2BClientError as error:
            raise UpstreamError(str(error)) from error

    def _send_blocked_event(
        self,
        product_id: str,
        hard_block: bool,
        reason_id: str,
        moderator_id: str,
        moderator_comment: str | None,
        field_reports: list[dict[str, Any]],
    ) -> None:
        try:
            self.b2b_client.send_moderation_event(
                {
                    "idempotency_key": str(uuid.uuid4()),
                    "product_id": product_id,
                    "event_type": "BLOCKED",
                    "occurred_at": utc_now(),
                    "moderator_id": moderator_id,
                    "moderator_comment": moderator_comment,
                    "blocking_reason_id": reason_id,
                    "hard_block": hard_block,
                    "field_reports": _b2b_field_reports(field_reports),
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
    comment = payload.get("comment", payload.get("moderator_comment"))
    if comment is None:
        return None
    if not isinstance(comment, str):
        raise ValidationError("comment must be a string")
    return comment


def _ticket_kind(json_before: Any) -> str:
    return "CREATE" if json_before is None else "EDIT"


def _decline_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")

    reason_ids = payload.get("blocking_reason_ids")
    if not isinstance(reason_ids, list) or len(reason_ids) == 0:
        raise ValidationError("blocking_reason_ids must be a non-empty array")
    blocking_reason_ids = [
        _validate_uuid(reason_id, "blocking_reason_ids[]")
        for reason_id in reason_ids
    ]

    moderator_comment = payload.get("comment")
    if moderator_comment is not None and not isinstance(moderator_comment, str):
        raise ValidationError("comment must be a string")
    if isinstance(moderator_comment, str) and len(moderator_comment) > 2000:
        raise ValidationError("comment must be at most 2000 characters")

    raw_reports = payload.get("field_reports", [])
    if not isinstance(raw_reports, list):
        raise ValidationError("field_reports must be an array")

    field_reports = []
    for report in raw_reports:
        if not isinstance(report, dict):
            raise ValidationError("field_reports items must be objects")
        field_path = report.get("field_path")
        if not isinstance(field_path, str) or not field_path.strip():
            raise ValidationError("field_reports.field_path is required")
        sku_id = report.get("sku_id")
        if sku_id is not None:
            sku_id = _validate_uuid(sku_id, "field_reports.sku_id")
        message = report.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValidationError("field_reports.message is required")
        if len(message) > 1000:
            raise ValidationError("field_reports.message must be at most 1000 characters")
        severity = report.get("severity", "ERROR")
        if severity not in FIELD_REPORT_SEVERITIES:
            raise ValidationError("field_reports.severity is invalid")
        field_reports.append(
            {
                "field_path": field_path,
                "sku_id": sku_id,
                "message": message,
                "severity": severity,
            }
        )

    return {
        "blocking_reason_ids": blocking_reason_ids,
        "moderator_comment": moderator_comment,
        "field_reports": field_reports,
    }


def _get_blocking_reasons(connection: Any, reason_ids: list[str]) -> list[Any]:
    placeholders = ", ".join("?" for _ in reason_ids)
    rows = connection.execute(
        f"""
        SELECT id, title, hard_block
        FROM product_blocking_reasons
        WHERE id IN ({placeholders})
        """,
        reason_ids,
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    return [by_id.get(reason_id) for reason_id in reason_ids]


def _primary_blocking_reason(reasons: list[Any]) -> Any:
    for reason in reasons:
        if bool(reason["hard_block"]):
            return reason
    return reasons[0]


def _b2b_field_reports(field_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "field_name": report["field_path"],
            "sku_id": report["sku_id"],
            "comment": report["message"],
        }
        for report in field_reports
    ]


def _ensure_assigned_for_decision(row: Any, moderator_id: str) -> None:
    if row is None:
        raise NotFoundError("Product not found in moderation queue")
    if row["status"] == "HARD_BLOCKED":
        raise ForbiddenError("Product is permanently blocked")
    if row["status"] != "IN_REVIEW":
        raise ConflictError("Product is not in review status")
    if row["moderator_id"] != moderator_id:
        raise ForbiddenError("This moderation card is not assigned to you")
