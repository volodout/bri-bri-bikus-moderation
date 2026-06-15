from __future__ import annotations

import json
import tempfile
import unittest
import uuid

from moderation.database import ModerationStore
from moderation.http_app import handle_product_event_post
from moderation.errors import BusinessError
from moderation.product_events import ProductEventService


PRODUCT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"


class FakeB2BClient:
    def __init__(self, product: dict):
        self.product = product
        self.calls: list[str] = []

    def fetch_product(self, product_id: str) -> dict:
        self.calls.append(product_id)
        return dict(self.product)


class ProductEventTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmpdir.name}/moderation.sqlite3"
        self.store = ModerationStore(self.db_path)
        self.store.ensure_schema()
        self.product = {
            "id": PRODUCT_ID,
            "title": "iPhone 15 Pro Max",
            "description": "Flagship smartphone",
            "status": "ON_MODERATION",
            "deleted": False,
            "blocked": False,
            "skus": [
                {
                    "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                    "name": "256GB Black",
                    "price": 12999000,
                    "cost_price": 10000000,
                    "reserved_quantity": 2,
                    "active_quantity": 10,
                }
            ],
            "field_reports": [],
        }

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def service(self, product: dict | None = None) -> tuple[ProductEventService, FakeB2BClient]:
        client = FakeB2BClient(product or self.product)
        return ProductEventService(self.store, client), client

    def event(
        self,
        event_type: str = "PRODUCT_CREATED",
        event_date: str = "2026-03-15T14:30:00.000Z",
        json_after: dict | None = None,
        json_before: dict | None = None,
    ) -> dict:
        payload = {
            "product_id": PRODUCT_ID,
            "seller_id": SELLER_ID,
        }
        if event_type == "PRODUCT_DELETED":
            payload = {"product_id": PRODUCT_ID}
        elif event_type == "PRODUCT_EDITED":
            payload["json_before"] = json_before or self.product
            payload["json_after"] = json_after or self.product
        else:
            payload["json_after"] = json_after or self.product

        return {
            "idempotency_key": str(uuid.uuid4()),
            "event_type": event_type,
            "occurred_at": event_date,
            "payload": payload,
        }

    def product_row(self):
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT * FROM product_moderation WHERE product_id = ?",
                (PRODUCT_ID,),
            ).fetchone()

    def test_created_event_uses_payload_snapshot_and_creates_pending_moderation_record(self) -> None:
        service, client = self.service()

        result = service.handle(self.event("PRODUCT_CREATED"))

        self.assertEqual({"status": "accepted"}, result.as_json())
        self.assertEqual([], client.calls)
        row = self.product_row()
        self.assertEqual("PENDING", row["status"])
        self.assertEqual(1, row["queue_priority"])
        self.assertIsNone(row["json_before"])
        self.assertEqual(10, row["total_active_quantity"])
        json_after = json.loads(row["json_after"])
        self.assertNotIn("cost_price", json_after["skus"][0])
        self.assertNotIn("reserved_quantity", json_after["skus"][0])

    def test_created_event_with_same_idempotency_key_is_noop(self) -> None:
        service, client = self.service()
        event = self.event("PRODUCT_CREATED")

        service.handle(event)
        service.handle(event)

        self.assertEqual([], client.calls)

    def test_duplicate_created_event_is_business_error(self) -> None:
        service, _ = self.service()
        service.handle(self.event("PRODUCT_CREATED", "2026-03-15T14:30:00.000Z"))

        with self.assertRaises(BusinessError):
            service.handle(self.event("PRODUCT_CREATED", "2026-03-15T14:31:00.000Z"))

    def test_edited_event_requeues_blocked_product_and_clears_field_reports(self) -> None:
        service, _ = self.service()
        service.handle(self.event("PRODUCT_CREATED", "2026-03-15T14:30:00.000Z"))
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM product_moderation WHERE product_id = ?",
                (PRODUCT_ID,),
            ).fetchone()
            connection.execute(
                """
                UPDATE product_moderation
                SET status = 'BLOCKED',
                    queue_priority = 2,
                    blocking_reason_id = ?,
                    moderator_id = ?,
                    date_moderation = ?
                WHERE product_id = ?
                """,
                (
                    "a7b8c9d0-1234-5678-ef01-890123456789",
                    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "2026-03-15T15:00:00.000Z",
                    PRODUCT_ID,
                ),
            )
            connection.execute(
                """
                INSERT INTO product_moderation_field_report (
                    id,
                    product_moderation_id,
                    field_name,
                    sku_id,
                    comment,
                    date_created
                )
                VALUES (?, ?, 'description', NULL, 'Fix description', ?)
                """,
                (str(uuid.uuid4()), row["id"], "2026-03-15T15:00:00.000Z"),
            )

        edited_product = dict(self.product)
        edited_product["description"] = "Updated description"
        service, _ = self.service(edited_product)

        service.handle(
            self.event(
                "PRODUCT_EDITED",
                "2026-03-16T09:00:00.000Z",
                json_before=self.product,
                json_after=edited_product,
            )
        )

        row = self.product_row()
        self.assertEqual("PENDING", row["status"])
        self.assertEqual(2, row["queue_priority"])
        self.assertIsNone(row["moderator_id"])
        self.assertEqual("Updated description", json.loads(row["json_after"])["description"])
        self.assertEqual("Flagship smartphone", json.loads(row["json_before"])["description"])
        with self.store.connect() as connection:
            reports_count = connection.execute(
                "SELECT COUNT(*) FROM product_moderation_field_report"
            ).fetchone()[0]
        self.assertEqual(0, reports_count)

    def test_edited_event_requeues_in_review_product_and_releases_moderator(self) -> None:
        service, _ = self.service()
        service.handle(self.event("PRODUCT_CREATED", "2026-03-15T14:30:00.000Z"))
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE product_moderation
                SET status = 'IN_REVIEW',
                    moderator_id = ?
                WHERE product_id = ?
                """,
                ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", PRODUCT_ID),
            )

        edited_product = dict(self.product)
        edited_product["title"] = "Updated title"
        service, _ = self.service(edited_product)

        service.handle(
            self.event(
                "PRODUCT_EDITED",
                "2026-03-16T09:00:00.000Z",
                json_before=self.product,
                json_after=edited_product,
            )
        )

        row = self.product_row()
        self.assertEqual("PENDING", row["status"])
        self.assertIsNone(row["moderator_id"])
        self.assertEqual(1, row["queue_priority"])
        self.assertEqual("Updated title", json.loads(row["json_after"])["title"])

    def test_deleted_event_removes_record_and_is_idempotent_when_missing(self) -> None:
        service, _ = self.service()
        service.handle(self.event("PRODUCT_CREATED", "2026-03-15T14:30:00.000Z"))

        service.handle(self.event("PRODUCT_DELETED", "2026-03-16T09:00:00.000Z"))
        service.handle(self.event("PRODUCT_DELETED", "2026-03-16T09:01:00.000Z"))

        self.assertIsNone(self.product_row())


class HttpProductEventTestCase(unittest.TestCase):
    def test_invalid_service_key_returns_401(self) -> None:
        store = ModerationStore(":memory:")
        service = ProductEventService(store, FakeB2BClient({}))
        body = json.dumps({"event_type": "PRODUCT_CREATED"}).encode("utf-8")

        status_code, payload = handle_product_event_post(
            "/api/v1/b2b/events",
            {"X-Service-Key": "bad-key"},
            body,
            service,
            "expected-key",
        )

        self.assertEqual(401, status_code)
        self.assertEqual({"code": "UNAUTHORIZED", "message": "Unauthorized"}, payload)

    def test_product_event_endpoint_returns_202_for_contract_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModerationStore(f"{tmpdir}/moderation.sqlite3")
            store.ensure_schema()
            service = ProductEventService(store, FakeB2BClient({}))
            body = json.dumps(
                {
                    "idempotency_key": str(uuid.uuid4()),
                    "event_type": "PRODUCT_CREATED",
                    "occurred_at": "2026-03-15T14:30:00.000Z",
                    "payload": {
                        "product_id": PRODUCT_ID,
                        "seller_id": SELLER_ID,
                        "json_after": {
                            "id": PRODUCT_ID,
                            "skus": [],
                        },
                    },
                }
            ).encode("utf-8")

            status_code, payload = handle_product_event_post(
                "/api/v1/b2b/events",
                {"X-Service-Key": "expected-key"},
                body,
                service,
                "expected-key",
            )

        self.assertEqual(202, status_code)
        self.assertIsNone(payload)

    def test_legacy_product_event_endpoint_is_not_mounted(self) -> None:
        status_code, payload = handle_product_event_post(
            "/api/v1/events/product",
            {"X-Service-Key": "expected-key"},
            b"{}",
            ProductEventService(ModerationStore(":memory:"), FakeB2BClient({})),
            "expected-key",
        )

        self.assertEqual(404, status_code)
        self.assertEqual({"code": "NOT_FOUND", "message": "Not found"}, payload)


if __name__ == "__main__":
    unittest.main()
