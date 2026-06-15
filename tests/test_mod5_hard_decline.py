from __future__ import annotations

import json
import tempfile
import unittest
import uuid

from moderation.b2b_client import B2BClientError
from moderation.database import ModerationStore
from moderation.decision_service import DecisionService
from moderation.errors import ForbiddenError
from moderation.product_events import ProductEventService


PRODUCT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
MODERATOR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
HARD_REASON_ID = "b4c5d6e7-8901-2345-5678-567890123456"


class FakeB2BClient:
    def __init__(self, fail_send: bool = False):
        self.fail_send = fail_send
        self.sent_events: list[dict] = []
        self.fetch_calls: list[str] = []

    def fetch_product(self, product_id: str) -> dict:
        self.fetch_calls.append(product_id)
        return {"id": product_id, "skus": [{"id": str(uuid.uuid4())}]}

    def send_moderation_event(self, payload: dict) -> None:
        if self.fail_send:
            raise B2BClientError("B2B returned HTTP 500")
        self.sent_events.append(payload)


class HardDeclineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmpdir.name}/moderation.sqlite3"
        self.store = ModerationStore(self.db_path)
        self.store.ensure_schema()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def insert_card(self, status: str = "IN_REVIEW") -> str:
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
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    moderation_id,
                    PRODUCT_ID,
                    SELLER_ID,
                    status,
                    json.dumps({"id": PRODUCT_ID, "skus": [{"id": str(uuid.uuid4())}]}),
                    MODERATOR_ID,
                    "2026-03-01T10:00:00.000Z",
                    "2026-03-01T10:00:00.000Z",
                ),
            )
        return moderation_id

    def row(self):
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT * FROM product_moderation WHERE product_id = ?",
                (PRODUCT_ID,),
            ).fetchone()

    def payload(self) -> dict:
        return {
            "blocking_reason_ids": [HARD_REASON_ID],
            "comment": "Counterfeit product confirmed",
            "field_reports": [],
        }

    def test_hard_reason_sets_hard_blocked_and_sends_hard_block_event(self) -> None:
        self.insert_card()
        client = FakeB2BClient()
        service = DecisionService(self.store, client)

        result = service.decline(PRODUCT_ID, MODERATOR_ID, self.payload())

        row = self.row()
        self.assertEqual({"product_id": PRODUCT_ID, "status": "HARD_BLOCKED"}, result.as_json())
        self.assertEqual("HARD_BLOCKED", row["status"])
        self.assertEqual(HARD_REASON_ID, row["blocking_reason_id"])
        self.assertEqual("Counterfeit product confirmed", row["moderator_comment"])
        event = client.sent_events[0]
        self.assertEqual("BLOCKED", event["event_type"])
        self.assertTrue(event["hard_block"])
        self.assertEqual(HARD_REASON_ID, event["blocking_reason_id"])
        self.assertEqual(MODERATOR_ID, event["moderator_id"])
        self.assertIn("occurred_at", event)
        self.assertEqual([], event["field_reports"])

    def test_failed_b2b_event_rolls_back_hard_block(self) -> None:
        self.insert_card()
        service = DecisionService(self.store, FakeB2BClient(fail_send=True))

        with self.assertRaises(Exception):
            service.decline(PRODUCT_ID, MODERATOR_ID, self.payload())

        self.assertEqual("IN_REVIEW", self.row()["status"])

    def test_any_modify_on_hard_blocked_returns_403(self) -> None:
        ticket_id = self.insert_card(status="HARD_BLOCKED")
        client = FakeB2BClient()
        service = DecisionService(self.store, client)

        with self.assertRaises(ForbiddenError):
            service.decline(PRODUCT_ID, MODERATOR_ID, self.payload())
        with self.assertRaises(ForbiddenError):
            service.block_ticket(ticket_id, MODERATOR_ID, self.payload())
        with self.assertRaises(ForbiddenError):
            service.approve(PRODUCT_ID, MODERATOR_ID, {"comment": "nope"})

        self.assertEqual([], client.fetch_calls)
        self.assertEqual([], client.sent_events)
        self.assertEqual("HARD_BLOCKED", self.row()["status"])

    def test_edited_event_on_hard_blocked_is_ignored(self) -> None:
        self.insert_card(status="HARD_BLOCKED")
        client = FakeB2BClient()
        service = ProductEventService(self.store, client)

        result = service.handle(
            {
                "idempotency_key": str(uuid.uuid4()),
                "event_type": "PRODUCT_EDITED",
                "occurred_at": "2026-03-16T09:00:00.000Z",
                "payload": {
                    "product_id": PRODUCT_ID,
                    "seller_id": SELLER_ID,
                    "json_before": {"id": PRODUCT_ID, "title": "Old"},
                    "json_after": {"id": PRODUCT_ID, "title": "New", "skus": []},
                },
            }
        )

        self.assertEqual({"status": "accepted"}, result.as_json())
        self.assertEqual([], client.fetch_calls)
        row = self.row()
        self.assertEqual("HARD_BLOCKED", row["status"])
        self.assertNotEqual("New", json.loads(row["json_after"]).get("title"))


if __name__ == "__main__":
    unittest.main()
