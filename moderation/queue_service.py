"""Business logic for MOD-2 get-next queue handling."""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Any

from moderation.database import ModerationStore
from moderation.errors import ValidationError
from moderation.product_events import utc_now


@dataclass(frozen=True)
class ModerationCard:
    product_moderation_id: str
    product_id: str
    seller_id: str
    status: str
    queue_priority: int
    json_before: dict[str, Any] | None
    json_after: dict[str, Any]
    blocking_history: dict[str, Any] | None
    date_created: str
    date_updated: str

    def as_json(self) -> dict[str, Any]:
        return {
            "product_moderation_id": self.product_moderation_id,
            "product_id": self.product_id,
            "seller_id": self.seller_id,
            "status": self.status,
            "queue_priority": self.queue_priority,
            "json_before": self.json_before,
            "json_after": self.json_after,
            "blocking_history": self.blocking_history,
            "date_created": self.date_created,
            "date_updated": self.date_updated,
        }


class QueueService:
    def __init__(self, store: ModerationStore):
        self.store = store

    def get_next(self, queue_id: Any, moderator_id: str) -> ModerationCard | None:
        queue_ids = _queue_scan_order(queue_id)
        moderator_id = _validate_uuid(moderator_id, "X-Moderator-Id")
        self.store.ensure_schema()
        now = utc_now()

        with self.store.transaction(immediate=True) as connection:
            for candidate_queue_id in queue_ids:
                row = connection.execute(
                    """
                    SELECT *
                    FROM product_moderation
                    WHERE status = 'PENDING'
                      AND queue_priority = ?
                    ORDER BY date_updated ASC
                    LIMIT 1
                    """,
                    (candidate_queue_id,),
                ).fetchone()
                if row is None:
                    continue

                connection.execute(
                    """
                    UPDATE product_moderation
                    SET status = 'IN_REVIEW',
                        moderator_id = ?,
                        date_updated = ?
                    WHERE id = ?
                    """,
                    (moderator_id, now, row["id"]),
                )
                updated = connection.execute(
                    """
                    SELECT *
                    FROM product_moderation
                    WHERE id = ?
                    """,
                    (row["id"],),
                ).fetchone()
                return _row_to_card(connection, updated)

        return None


def _queue_scan_order(queue_id: Any) -> list[int]:
    if queue_id is None:
        return [1, 2, 3, 4]
    if isinstance(queue_id, bool) or not isinstance(queue_id, int):
        raise ValidationError("queueId must be an integer from 1 to 4")
    if queue_id < 1 or queue_id > 4:
        raise ValidationError("queueId must be an integer from 1 to 4")
    return [queue_id]


def _validate_uuid(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ValidationError(f"{field_name} must be a UUID") from error


def _row_to_card(connection: Any, row: Any) -> ModerationCard:
    json_before = json.loads(row["json_before"]) if row["json_before"] else None
    json_after = json.loads(row["json_after"])
    return ModerationCard(
        product_moderation_id=row["id"],
        product_id=row["product_id"],
        seller_id=row["seller_id"],
        status=row["status"],
        queue_priority=row["queue_priority"],
        json_before=json_before,
        json_after=json_after,
        blocking_history=_blocking_history(connection, row, json_before),
        date_created=row["date_created"],
        date_updated=row["date_updated"],
    )


def _blocking_history(connection: Any, row: Any, json_before: dict[str, Any] | None) -> dict[str, Any] | None:
    if row["blocking_reason_id"] is None and row["moderator_comment"] is None:
        return None

    reason = None
    field_reports: list[dict[str, Any]] = []
    if json_before is not None:
        before_reason = json_before.get("blocking_reason")
        if isinstance(before_reason, dict):
            reason = {
                "id": before_reason.get("id", row["blocking_reason_id"]),
                "title": before_reason.get("title"),
            }
            if "comment" in before_reason and row["moderator_comment"] is None:
                moderator_comment = before_reason["comment"]
            else:
                moderator_comment = row["moderator_comment"]
        else:
            moderator_comment = row["moderator_comment"]

        before_reports = json_before.get("field_reports")
        if isinstance(before_reports, list):
            field_reports = [
                report
                for report in before_reports
                if isinstance(report, dict)
            ]
    else:
        moderator_comment = row["moderator_comment"]

    if reason is None and row["blocking_reason_id"] is not None:
        reason_row = connection.execute(
            """
            SELECT id, title
            FROM product_blocking_reasons
            WHERE id = ?
            """,
            (row["blocking_reason_id"],),
        ).fetchone()
        if reason_row is not None:
            reason = {"id": reason_row["id"], "title": reason_row["title"]}
        else:
            reason = {"id": row["blocking_reason_id"], "title": None}

    return {
        "blocking_reason": reason,
        "moderator_comment": moderator_comment,
        "field_reports": field_reports,
        "date_blocked": row["date_moderation"],
    }

