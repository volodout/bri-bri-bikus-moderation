from __future__ import annotations

import inspect
import json
import tempfile
import unittest
import uuid

from moderation.database import ModerationStore
from moderation.errors import ConflictError, ValidationError
from moderation.http_app import handle_get_next_post
from moderation.postgres_database import PostgresModerationStore
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
        moderator_id: str | None = None,
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
                    moderator_id,
                    date_created,
                    date_updated,
                    date_moderation
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    moderator_id,
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

    def test_queue_priority_claims_oldest_pending_card(self) -> None:
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

    def test_claim_response_uses_ticket_response_shape(self) -> None:
        older_id = self.insert_card(
            "11111111-1111-1111-1111-111111111111",
            1,
            "2026-03-01T10:00:00.000Z",
        )

        card = self.service.get_next(1, MODERATOR_ID)

        self.assertEqual(
            {
                "id": older_id,
                "product_id": "11111111-1111-1111-1111-111111111111",
                "seller_id": SELLER_ID,
                "category_id": None,
                "kind": "CREATE",
                "status": "IN_REVIEW",
                "queue_priority": 1,
                "assigned_moderator_id": MODERATOR_ID,
                "claimed_at": card.date_updated,
                "claim_expires_at": None,
                "decision_at": None,
                "created_at": "2026-03-01T10:00:00.000Z",
                "updated_at": card.date_updated,
                "json_before": None,
                "json_after": {"id": "11111111-1111-1111-1111-111111111111", "skus": []},
                "blocking_history": None,
            },
            card.as_json(),
        )

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

    def test_store_claim_method_is_used_for_postgres_skip_locked(self) -> None:
        store = FakeClaimStore()
        service = QueueService(store)

        card = service.get_next(None, MODERATOR_ID)

        self.assertEqual([1, 2, 3, 4], store.claimed_queue_ids)
        self.assertEqual(MODERATOR_ID, store.claimed_moderator_id)
        self.assertEqual("IN_REVIEW", card.status)
        self.assertEqual("99999999-9999-9999-9999-999999999999", card.product_moderation_id)

    def test_postgres_claim_uses_for_update_skip_locked(self) -> None:
        source = inspect.getsource(PostgresModerationStore.claim_next_pending_card)

        self.assertIn("FOR UPDATE SKIP LOCKED", source)

    def test_moderator_already_has_in_review_returns_409(self) -> None:
        held_id = self.insert_card(
            "11111111-1111-1111-1111-111111111111",
            1,
            "2026-03-01T10:00:00.000Z",
            status="IN_REVIEW",
            moderator_id=MODERATOR_ID,
        )
        pending_id = self.insert_card(
            "22222222-2222-2222-2222-222222222222",
            1,
            "2026-03-01T11:00:00.000Z",
        )

        with self.assertRaises(ConflictError):
            self.service.get_next(None, MODERATOR_ID)

        self.assertEqual("IN_REVIEW", self.row(held_id)["status"])
        self.assertEqual("PENDING", self.row(pending_id)["status"])

    def test_invalid_queue_id_is_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.get_next(5, MODERATOR_ID)

    def test_http_get_next_returns_204_when_empty(self) -> None:
        status_code, payload = handle_get_next_post(
            "/api/v1/queue/claim",
            {"X-Moderator-Id": MODERATOR_ID},
            b"{}",
            self.service,
        )

        self.assertEqual(204, status_code)
        self.assertIsNone(payload)

    def test_http_claim_reads_queue_priority(self) -> None:
        queue_1_id = self.insert_card(
            "11111111-1111-1111-1111-111111111111",
            1,
            "2026-03-01T10:00:00.000Z",
        )
        queue_2_id = self.insert_card(
            "22222222-2222-2222-2222-222222222222",
            2,
            "2026-03-01T09:00:00.000Z",
        )

        status_code, payload = handle_get_next_post(
            "/api/v1/queue/claim",
            {"X-Moderator-Id": MODERATOR_ID},
            b'{"queue_priority": 2}',
            self.service,
        )

        self.assertEqual(200, status_code)
        self.assertEqual(queue_2_id, payload["id"])
        self.assertEqual("PENDING", self.row(queue_1_id)["status"])

    def test_http_legacy_get_next_route_is_not_mounted(self) -> None:
        status_code, payload = handle_get_next_post(
            "/api/v1/product-moderation/get-next",
            {"X-Moderator-Id": MODERATOR_ID},
            b"{}",
            self.service,
        )

        self.assertEqual(404, status_code)
        self.assertEqual({"code": "NOT_FOUND", "message": "Not found"}, payload)

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
                    {"field_path": "description", "sku_id": None, "message": "Too vague"}
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
                    {"field_path": "description", "sku_id": None, "message": "Too vague"}
                ],
                "date_blocked": "2026-03-01T09:00:00.000Z",
            },
            card.blocking_history,
        )


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


class FakeClaimStore:
    def __init__(self):
        self.claimed_queue_ids = None
        self.claimed_moderator_id = None

    def ensure_schema(self) -> None:
        return None

    def claim_next_pending_card(self, queue_ids: list[int], moderator_id: str, now: str):
        self.claimed_queue_ids = queue_ids
        self.claimed_moderator_id = moderator_id
        return {
            "id": "99999999-9999-9999-9999-999999999999",
            "product_id": "11111111-1111-1111-1111-111111111111",
            "seller_id": SELLER_ID,
            "status": "IN_REVIEW",
            "queue_priority": 1,
            "moderator_id": moderator_id,
            "json_before": None,
            "json_after": json.dumps(
                {"id": "11111111-1111-1111-1111-111111111111", "skus": []},
                separators=(",", ":"),
            ),
            "blocking_reason_id": None,
            "moderator_comment": None,
            "date_created": "2026-03-01T10:00:00.000Z",
            "date_updated": now,
            "date_moderation": None,
        }

    def connect(self) -> FakeConnection:
        return FakeConnection()


if __name__ == "__main__":
    unittest.main()
