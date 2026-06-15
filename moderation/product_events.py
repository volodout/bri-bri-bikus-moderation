"""Business logic for MOD-1 product events from B2B."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid
from typing import Any

from moderation.b2b_client import B2BClientError
from moderation.database import ModerationStore
from moderation.errors import BusinessError, UpstreamError, ValidationError


PRODUCT_EVENT_SENDER = "b2b"
PRODUCT_EVENTS = {"CREATED", "EDITED", "DELETED"}
PRODUCT_EVENT_TYPES = {
    "PRODUCT_CREATED": "CREATED",
    "PRODUCT_EDITED": "EDITED",
    "PRODUCT_DELETED": "DELETED",
}


@dataclass(frozen=True)
class ProductEvent:
    idempotency_key: str
    product_id: str
    seller_id: str | None
    event: str
    event_date: str
    json_after: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> "ProductEvent":
        if not isinstance(payload, dict):
            raise ValidationError("Request body must be a JSON object")

        idempotency_key = _required_uuid(payload, "idempotency_key")
        event_type = payload.get("event_type")
        event = PRODUCT_EVENT_TYPES.get(event_type)
        if event is None:
            raise ValidationError(
                "event_type must be one of PRODUCT_CREATED, PRODUCT_EDITED, PRODUCT_DELETED"
            )

        event_date = payload.get("occurred_at")
        if not isinstance(event_date, str):
            raise ValidationError("occurred_at is required")
        event_date = normalize_iso_datetime(event_date, field_name="occurred_at")

        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            raise ValidationError("payload is required")

        product_id = _required_uuid(event_payload, "product_id")
        seller_id = None if event == "DELETED" else _required_uuid(event_payload, "seller_id")
        json_after = None
        if event in {"CREATED", "EDITED"}:
            json_after = _required_object(event_payload, "json_after")
        if event == "EDITED":
            _required_object(event_payload, "json_before")

        return cls(
            idempotency_key=idempotency_key,
            product_id=product_id,
            seller_id=seller_id,
            event=event,
            event_date=event_date,
            json_after=json_after,
        )


@dataclass(frozen=True)
class EventResult:
    status: str = "accepted"

    def as_json(self) -> dict[str, str]:
        return {"status": self.status}


class ProductEventService:
    def __init__(self, store: ModerationStore, b2b_client: Any):
        self.store = store
        self.b2b_client = b2b_client

    def handle(self, payload: Any) -> EventResult:
        event = ProductEvent.from_payload(payload)
        self.store.ensure_schema()

        with self.store.transaction() as connection:
            if _processed_event_exists(connection, event.idempotency_key):
                return EventResult()

            existing = _get_moderation_by_product_id(connection, event.product_id)
            if _is_stale_event(existing, event.event_date):
                _record_processed_event(connection, event.idempotency_key, {"status": "accepted"})
                return EventResult()

            if event.event == "DELETED":
                if existing is not None:
                    connection.execute(
                        "DELETE FROM product_moderation WHERE product_id = ?",
                        (event.product_id,),
                    )
                _record_processed_event(connection, event.idempotency_key, {"status": "accepted"})
                return EventResult()

            if event.event == "CREATED":
                if existing is not None and existing["status"] == "HARD_BLOCKED":
                    _record_processed_event(connection, event.idempotency_key, {"status": "accepted"})
                    return EventResult()
                if existing is not None:
                    raise BusinessError("Duplicate CREATED event for product")

            if event.event == "EDITED":
                if existing is None:
                    raise BusinessError("Product moderation record not found")
                if existing["status"] == "HARD_BLOCKED":
                    _record_processed_event(connection, event.idempotency_key, {"status": "accepted"})
                    return EventResult()

        product = event.json_after if event.json_after is not None else self._fetch_product(event.product_id)
        product = strip_private_fields(product)
        total_active_quantity = calculate_total_active_quantity(product)
        product_json = json.dumps(product, ensure_ascii=False, separators=(",", ":"))
        now = utc_now()

        with self.store.transaction() as connection:
            existing = _get_moderation_by_product_id(connection, event.product_id)
            if event.event == "CREATED":
                if existing is not None and existing["status"] == "HARD_BLOCKED":
                    _record_processed_event(connection, event.idempotency_key, {"status": "accepted"})
                    return EventResult()
                if existing is not None:
                    raise BusinessError("Duplicate CREATED event for product")
                connection.execute(
                    """
                    INSERT INTO product_moderation (
                        id,
                        product_id,
                        seller_id,
                        status,
                        queue_priority,
                        json_before,
                        json_after,
                        total_active_quantity,
                        date_created,
                        date_updated,
                        last_event_date
                    )
                    VALUES (?, ?, ?, 'PENDING', 1, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        event.product_id,
                        event.seller_id,
                        product_json,
                        total_active_quantity,
                        now,
                        now,
                        event.event_date,
                    ),
                )
            else:
                if existing is None:
                    raise BusinessError("Product moderation record not found")
                if existing["status"] == "HARD_BLOCKED":
                    _record_processed_event(connection, event.idempotency_key, {"status": "accepted"})
                    return EventResult()

                queue_priority = _next_queue_priority(existing, total_active_quantity)
                connection.execute(
                    """
                    UPDATE product_moderation
                    SET seller_id = ?,
                        json_before = json_after,
                        json_after = ?,
                        status = 'PENDING',
                        queue_priority = ?,
                        total_active_quantity = ?,
                        moderator_id = NULL,
                        date_updated = ?,
                        last_event_date = ?
                    WHERE product_id = ?
                    """,
                    (
                        event.seller_id,
                        product_json,
                        queue_priority,
                        total_active_quantity,
                        now,
                        event.event_date,
                        event.product_id,
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM product_moderation_field_report
                    WHERE product_moderation_id = ?
                    """,
                    (existing["id"],),
                )

            _record_processed_event(connection, event.idempotency_key, {"status": "accepted"})

        return EventResult()

    def _fetch_product(self, product_id: str) -> dict[str, Any]:
        try:
            return self.b2b_client.fetch_product(product_id)
        except B2BClientError as error:
            raise UpstreamError(str(error)) from error


def strip_private_fields(product_data: dict[str, Any]) -> dict[str, Any]:
    product_copy = dict(product_data)
    skus = product_copy.get("skus")
    if isinstance(skus, list):
        stripped_skus = []
        for sku in skus:
            if isinstance(sku, dict):
                sku_copy = dict(sku)
                sku_copy.pop("cost_price", None)
                sku_copy.pop("reserved_quantity", None)
                sku_copy.pop("costPrice", None)
                sku_copy.pop("reservedQuantity", None)
                stripped_skus.append(sku_copy)
            else:
                stripped_skus.append(sku)
        product_copy["skus"] = stripped_skus
    return product_copy


def calculate_total_active_quantity(product_data: dict[str, Any]) -> int:
    total = 0
    skus = product_data.get("skus")
    if not isinstance(skus, list):
        return total
    for sku in skus:
        if not isinstance(sku, dict):
            continue
        quantity = sku.get("active_quantity", sku.get("activeQuantity", 0))
        if isinstance(quantity, bool):
            continue
        if isinstance(quantity, int):
            total += max(quantity, 0)
        elif isinstance(quantity, str) and quantity.isdigit():
            total += int(quantity)
    return total


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_iso_datetime(value: str, field_name: str) -> str:
    raw_value = value.strip()
    parse_value = raw_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(parse_value)
    except ValueError as error:
        raise ValidationError(f"{field_name} must be ISO 8601") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _required_uuid(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} is required")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ValidationError(f"{field_name} must be a UUID") from error


def _required_object(payload: dict[str, Any], field_name: str) -> dict[str, Any]:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise ValidationError(f"{field_name} is required")
    return value


def _processed_event_exists(connection: Any, idempotency_key: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM processed_events
        WHERE sender_service = ? AND idempotency_key = ?
        """,
        (PRODUCT_EVENT_SENDER, idempotency_key),
    ).fetchone()
    return row is not None


def _record_processed_event(connection: Any, idempotency_key: str, response: dict[str, str]) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO processed_events (
            id,
            sender_service,
            idempotency_key,
            response_cached,
            processed_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            PRODUCT_EVENT_SENDER,
            idempotency_key,
            json.dumps(response, separators=(",", ":")),
            utc_now(),
        ),
    )


def _get_moderation_by_product_id(connection: Any, product_id: str) -> Any:
    return connection.execute(
        """
        SELECT *
        FROM product_moderation
        WHERE product_id = ?
        """,
        (product_id,),
    ).fetchone()


def _is_stale_event(existing: Any, event_date: str) -> bool:
    if existing is None or existing["last_event_date"] is None:
        return False
    return event_date <= existing["last_event_date"]


def _next_queue_priority(existing: Any, total_active_quantity: int) -> int:
    old_status = existing["status"]
    if old_status == "BLOCKED":
        return 2
    if old_status == "MODERATED":
        return 3 if total_active_quantity > 0 else 4
    return int(existing["queue_priority"])
