from __future__ import annotations

import json
import tempfile
import unittest
import uuid

from moderation.b2b_client import B2BClientError
from moderation.database import ModerationStore
from moderation.decision_service import DecisionService
from moderation.errors import BusinessError, ValidationError
from moderation.http_app import handle_decline_post


PRODUCT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
MODERATOR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SOFT_REASON_ID = "a7b8c9d0-1234-5678-ef01-890123456789"
HARD_REASON_ID = "b4c5d6e7-8901-2345-5678-567890123456"


class FakeB2BClient:
    def __init__(self, fail_send: bool = False):
        self.fail_send = fail_send
        self.sent_events: list[dict] = []

    def fetch_product(self, product_id: str) -> dict:
        return {"id": product_id, "skus": [{"id": str(uuid.uuid4())}]}

    def send_moderation_event(self, payload: dict) -> None:
        if self.fail_send:
            raise B2BClientError("B2B returned HTTP 500")
        self.sent_events.append(payload)


class DeclineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmpdir.name}/moderation.sqlite3"
        self.store = ModerationStore(self.db_path)
        self.store.ensure_schema()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def insert_card(self) -> str:
        moderation_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO product_moderation (
                    id,
                    product_id,
                    seller_id,
                    status,
                    queue_priority,
                    json_after,
                    moderator_id,
                    date_created,
                    date_updated
                )
                VALUES (?, ?, ?, 'IN_REVIEW', 1, ?, ?, ?, ?)
                """,
                (
                    moderation_id,
                    PRODUCT_ID,
                    SELLER_ID,
                    json.dumps({"id": PRODUCT_ID, "skus": [{"id": str(uuid.uuid4())}]}),
                    MODERATOR_ID,
                    "2026-03-01T10:00:00.000Z",
                    "2026-03-01T10:00:00.000Z",
                ),
            )
        return moderation_id

    def decline_payload(self) -> dict:
        return {
            "blocking_reason_id": SOFT_REASON_ID,
            "moderator_comment": "Description and photos do not match",
            "field_reports": [
                {
                    "field_name": "description",
                    "sku_id": None,
                    "comment": "Description belongs to another product",
                },
                {
                    "field_name": "sku_price",
                    "sku_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                    "comment": "Suspiciously low price",
                },
            ],
        }

    def row(self):
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT * FROM product_moderation WHERE product_id = ?",
                (PRODUCT_ID,),
            ).fetchone()

    def reports(self) -> list[dict]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT field_name, sku_id, comment
                FROM product_moderation_field_report
                ORDER BY field_name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def test_decline_soft_blocks_product_inserts_reports_and_sends_event(self) -> None:
        self.insert_card()
        client = FakeB2BClient()
        service = DecisionService(self.store, client)

        result = service.decline(PRODUCT_ID, MODERATOR_ID, self.decline_payload())

        row = self.row()
        self.assertEqual({"product_id": PRODUCT_ID, "status": "BLOCKED"}, result.as_json())
        self.assertEqual("BLOCKED", row["status"])
        self.assertEqual(SOFT_REASON_ID, row["blocking_reason_id"])
        self.assertEqual("Description and photos do not match", row["moderator_comment"])
        self.assertEqual(2, len(self.reports()))
        event = client.sent_events[0]
        self.assertEqual("BLOCKED", event["status"])
        self.assertFalse(event["hard_block"])
        self.assertEqual(SOFT_REASON_ID, event["blocking_reason"]["id"])
        self.assertEqual(2, len(event["field_reports"]))

    def test_decline_rejects_unknown_reason(self) -> None:
        self.insert_card()
        service = DecisionService(self.store, FakeB2BClient())
        payload = self.decline_payload()
        payload["blocking_reason_id"] = "99999999-9999-9999-9999-999999999999"

        with self.assertRaises(BusinessError):
            service.decline(PRODUCT_ID, MODERATOR_ID, payload)

    def test_decline_validates_field_reports(self) -> None:
        self.insert_card()
        service = DecisionService(self.store, FakeB2BClient())
        payload = self.decline_payload()
        payload["field_reports"][0]["field_name"] = "productImages"

        with self.assertRaises(ValidationError):
            service.decline(PRODUCT_ID, MODERATOR_ID, payload)

    def test_failed_b2b_event_rolls_back_soft_block(self) -> None:
        self.insert_card()
        service = DecisionService(self.store, FakeB2BClient(fail_send=True))

        with self.assertRaises(Exception):
            service.decline(PRODUCT_ID, MODERATOR_ID, self.decline_payload())

        self.assertEqual("IN_REVIEW", self.row()["status"])
        self.assertEqual([], self.reports())

    def test_http_decline_returns_response_body(self) -> None:
        self.insert_card()
        service = DecisionService(self.store, FakeB2BClient())

        status_code, payload = handle_decline_post(
            f"/api/v1/products/{PRODUCT_ID}/decline",
            {"X-Moderator-Id": MODERATOR_ID},
            json.dumps(self.decline_payload()).encode("utf-8"),
            service,
        )

        self.assertEqual(200, status_code)
        self.assertEqual({"product_id": PRODUCT_ID, "status": "BLOCKED"}, payload)


if __name__ == "__main__":
    unittest.main()
