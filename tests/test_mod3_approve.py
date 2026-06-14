from __future__ import annotations

import json
import tempfile
import unittest
import uuid

from moderation.b2b_client import B2BClientError
from moderation.database import ModerationStore
from moderation.decision_service import DecisionService
from moderation.errors import ConflictError, ForbiddenError
from moderation.http_app import handle_approve_post


PRODUCT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
MODERATOR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class FakeB2BClient:
    def __init__(self, product: dict | None = None, fail_send: bool = False):
        self.product = product or {"id": PRODUCT_ID, "skus": [{"id": str(uuid.uuid4())}]}
        self.fail_send = fail_send
        self.sent_events: list[dict] = []

    def fetch_product(self, product_id: str) -> dict:
        return dict(self.product)

    def send_moderation_event(self, payload: dict) -> None:
        if self.fail_send:
            raise B2BClientError("B2B returned HTTP 500")
        self.sent_events.append(payload)


class ApproveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmpdir.name}/moderation.sqlite3"
        self.store = ModerationStore(self.db_path)
        self.store.ensure_schema()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def insert_card(
        self,
        status: str = "IN_REVIEW",
        moderator_id: str | None = MODERATOR_ID,
        blocking_reason_id: str | None = "a7b8c9d0-1234-5678-ef01-890123456789",
    ) -> str:
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
                    blocking_reason_id,
                    moderator_id,
                    date_created,
                    date_updated
                )
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    moderation_id,
                    PRODUCT_ID,
                    SELLER_ID,
                    status,
                    json.dumps({"id": PRODUCT_ID, "skus": [{"id": str(uuid.uuid4())}]}),
                    blocking_reason_id,
                    moderator_id,
                    "2026-03-01T10:00:00.000Z",
                    "2026-03-01T10:00:00.000Z",
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
                VALUES (?, ?, 'description', NULL, 'Fix it', ?)
                """,
                (str(uuid.uuid4()), moderation_id, "2026-03-01T10:00:00.000Z"),
            )
        return moderation_id

    def row(self):
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT * FROM product_moderation WHERE product_id = ?",
                (PRODUCT_ID,),
            ).fetchone()

    def field_report_count(self) -> int:
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM product_moderation_field_report"
            ).fetchone()[0]

    def test_approve_updates_status_clears_reports_and_sends_event(self) -> None:
        self.insert_card()
        client = FakeB2BClient()
        service = DecisionService(self.store, client)

        result = service.approve(
            PRODUCT_ID,
            MODERATOR_ID,
            {"moderator_comment": "Looks good"},
        )

        row = self.row()
        self.assertEqual({"product_id": PRODUCT_ID, "status": "MODERATED"}, result.as_json())
        self.assertEqual("MODERATED", row["status"])
        self.assertEqual("Looks good", row["moderator_comment"])
        self.assertIsNone(row["blocking_reason_id"])
        self.assertIsNotNone(row["date_moderation"])
        self.assertEqual(0, self.field_report_count())
        self.assertEqual("MODERATED", client.sent_events[0]["status"])
        self.assertEqual(PRODUCT_ID, client.sent_events[0]["product_id"])
        self.assertIn("idempotency_key", client.sent_events[0])

    def test_approve_rejects_card_assigned_to_another_moderator(self) -> None:
        self.insert_card(moderator_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        service = DecisionService(self.store, FakeB2BClient())

        with self.assertRaises(ForbiddenError):
            service.approve(PRODUCT_ID, MODERATOR_ID, {})

    def test_approve_rejects_product_not_in_review(self) -> None:
        self.insert_card(status="PENDING", moderator_id=None)
        service = DecisionService(self.store, FakeB2BClient())

        with self.assertRaises(ConflictError):
            service.approve(PRODUCT_ID, MODERATOR_ID, {})

    def test_approve_rejects_product_without_skus(self) -> None:
        self.insert_card()
        client = FakeB2BClient({"id": PRODUCT_ID, "skus": []})
        service = DecisionService(self.store, client)

        with self.assertRaises(ConflictError):
            service.approve(PRODUCT_ID, MODERATOR_ID, {})

        self.assertEqual("IN_REVIEW", self.row()["status"])
        self.assertEqual([], client.sent_events)

    def test_failed_b2b_event_rolls_back_status(self) -> None:
        self.insert_card()
        service = DecisionService(self.store, FakeB2BClient(fail_send=True))

        with self.assertRaises(Exception):
            service.approve(PRODUCT_ID, MODERATOR_ID, {})

        self.assertEqual("IN_REVIEW", self.row()["status"])
        self.assertEqual(1, self.field_report_count())

    def test_http_approve_returns_response_body(self) -> None:
        self.insert_card()
        service = DecisionService(self.store, FakeB2BClient())

        status_code, payload = handle_approve_post(
            f"/api/v1/products/{PRODUCT_ID}/approve",
            {"X-Moderator-Id": MODERATOR_ID},
            b'{"moderator_comment":"ok"}',
            service,
        )

        self.assertEqual(200, status_code)
        self.assertEqual({"product_id": PRODUCT_ID, "status": "MODERATED"}, payload)


if __name__ == "__main__":
    unittest.main()

