from __future__ import annotations

import json
import tempfile
import unittest
import uuid

from moderation.database import ModerationStore
from moderation.errors import ValidationError
from moderation.http_app import handle_get_next_post
from moderation.queue_service import QueueService


MODERATOR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"


class GetNextTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmpdir.name}/moderation.sqlite3"
        self.store = ModerationStore(self.db_path)
        self.store.ensure_schema()
        self.service = QueueService(self.store)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def insert_card(
        self,
        product_id: str,
        queue_priority: int,
        date_updated: str,
        status: str = "PENDING",
        json_before: dict | None = None,
        json_after: dict | None = None,
        blocking_reason_id: str | None = None,
        moderator_comment: str | None = None,
        date_moderation: str | None = None,
    ) -> str:
        moderation_id = str(uuid.uuid4())
        json_after = json_after or {"id": product_id, "skus": []}
        with self.store.transaction() as connection:
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
                    blocking_reason_id,
                    moderator_comment,
                    date_created,
                    date_updated,
                    date_moderation
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    moderation_id,
                    product_id,
                    SELLER_ID,
                    status,
                    queue_priority,
                    json.dumps(json_before, separators=(",", ":")) if json_before else None,
                    json.dumps(json_after, separators=(",", ":")),
                    blocking_reason_id,
                    moderator_comment,
                    "2026-03-01T10:00:00.000Z",
                    date_updated,
                    date_moderation,
                ),
            )
        return moderation_id

    def row(self, moderation_id: str):
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT * FROM product_moderation WHERE id = ?",
                (moderation_id,),
            ).fetchone()

    def test_queue_id_claims_oldest_pending_card(self) -> None:
        older_id = self.insert_card(
            "11111111-1111-1111-1111-111111111111",
            1,
            "2026-03-01T10:00:00.000Z",
        )
        newer_id = self.insert_card(
            "22222222-2222-2222-2222-222222222222",
            1,
            "2026-03-01T11:00:00.000Z",
        )

        card = self.service.get_next(1, MODERATOR_ID)

        self.assertEqual(older_id, card.product_moderation_id)
        self.assertEqual("IN_REVIEW", card.status)
        self.assertEqual(MODERATOR_ID, self.row(older_id)["moderator_id"])
        self.assertEqual("PENDING", self.row(newer_id)["status"])

    def test_auto_priority_scans_queues_from_1_to_4(self) -> None:
        queue_4_id = self.insert_card(
            "44444444-4444-4444-4444-444444444444",
            4,
            "2026-03-01T10:00:00.000Z",
        )
        queue_2_id = self.insert_card(
            "22222222-2222-2222-2222-222222222222",
            2,
            "2026-03-01T11:00:00.000Z",
        )

        card = self.service.get_next(None, MODERATOR_ID)

        self.assertEqual(queue_2_id, card.product_moderation_id)
        self.assertEqual("PENDING", self.row(queue_4_id)["status"])

    def test_empty_queue_returns_none(self) -> None:
        self.assertIsNone(self.service.get_next(None, MODERATOR_ID))

    def test_invalid_queue_id_is_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.get_next(5, MODERATOR_ID)

    def test_http_get_next_returns_204_when_empty(self) -> None:
        status_code, payload = handle_get_next_post(
            "/api/v1/product-moderation/get-next",
            {"X-Moderator-Id": MODERATOR_ID},
            b"{}",
            self.service,
        )

        self.assertEqual(204, status_code)
        self.assertIsNone(payload)

    def test_blocking_history_comes_from_previous_snapshot(self) -> None:
        reason_id = "a7b8c9d0-1234-5678-ef01-890123456789"
        product_id = "33333333-3333-3333-3333-333333333333"
        self.insert_card(
            product_id,
            2,
            "2026-03-01T10:00:00.000Z",
            json_before={
                "id": product_id,
                "blocking_reason": {"id": reason_id, "title": "Bad description"},
                "field_reports": [
                    {"field_name": "description", "sku_id": None, "comment": "Too vague"}
                ],
            },
            blocking_reason_id=reason_id,
            moderator_comment="Fix the description",
            date_moderation="2026-03-01T09:00:00.000Z",
        )

        card = self.service.get_next(2, MODERATOR_ID)

        self.assertEqual(
            {
                "blocking_reason": {"id": reason_id, "title": "Bad description"},
                "moderator_comment": "Fix the description",
                "field_reports": [
                    {"field_name": "description", "sku_id": None, "comment": "Too vague"}
                ],
                "date_blocked": "2026-03-01T09:00:00.000Z",
            },
            card.blocking_history,
        )


if __name__ == "__main__":
    unittest.main()

