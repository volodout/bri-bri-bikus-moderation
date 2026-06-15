from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
import sqlite3
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from moderation.database import ModerationStore
from moderation.http_app import make_handler
from moderation.reference_service import ReferenceService


class BlockingReasonsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmpdir.name}/moderation.sqlite3"
        self.store = ModerationStore(self.db_path)
        self.store.ensure_schema()
        self.service = ReferenceService(self.store)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_blocking_reasons_returns_canonical_seed(self) -> None:
        reasons = self.service.blocking_reasons()

        self.assertEqual(10, len(reasons))
        self.assertEqual(
            {
                "id": "a7b8c9d0-1234-5678-ef01-890123456789",
                "code": "DESCRIPTION_MISMATCH",
                "title": "Описание не соответствует товару",
                "hard_block": False,
                "is_active": True,
            },
            reasons[0],
        )
        self.assertEqual(
            {
                "id": "d6e7f8a9-0123-4567-7890-789012345678",
                "code": "COPYRIGHT_VIOLATION",
                "title": "Товар нарушает авторские права",
                "hard_block": True,
                "is_active": True,
            },
            reasons[-1],
        )

    def test_inactive_reasons_not_visible(self) -> None:
        inactive_id = "11111111-2222-3333-4444-555555555555"
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO product_blocking_reasons (
                    id,
                    code,
                    title,
                    hard_block,
                    is_active
                )
                VALUES (?, 'INACTIVE_REASON', 'Inactive reason', 0, 0)
                """,
                (inactive_id,),
            )

        reasons = self.service.blocking_reasons()

        self.assertNotIn(inactive_id, {reason["id"] for reason in reasons})

    def test_unknown_active_reasons_are_sorted_after_seed(self) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO product_blocking_reasons (
                    id,
                    code,
                    title,
                    hard_block,
                    is_active
                )
                VALUES (?, 'ZZ_CUSTOM', 'Явная кастомная причина', 0, 1)
                """,
                ("11111111-2222-3333-4444-555555555555",),
            )

        reasons = self.service.blocking_reasons()

        self.assertEqual("ZZ_CUSTOM", reasons[-1]["code"])

    def test_old_blocking_reason_schema_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/moderation.sqlite3"
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE product_blocking_reasons (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        hard_block INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO product_blocking_reasons (id, title, hard_block)
                    VALUES (
                        'a7b8c9d0-1234-5678-ef01-890123456789',
                        'Описание не соответствует товару',
                        0
                    );
                    INSERT INTO product_blocking_reasons (id, title, hard_block)
                    VALUES (
                        '11111111-2222-3333-4444-555555555555',
                        'Явная кастомная причина',
                        0
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()

            service = ReferenceService(ModerationStore(db_path))
            reasons = service.blocking_reasons()

        by_id = {reason["id"]: reason for reason in reasons}
        self.assertEqual(
            "DESCRIPTION_MISMATCH",
            by_id["a7b8c9d0-1234-5678-ef01-890123456789"]["code"],
        )
        self.assertEqual(
            "CUSTOM_111111112222",
            by_id["11111111-2222-3333-4444-555555555555"]["code"],
        )
        self.assertTrue(by_id["11111111-2222-3333-4444-555555555555"]["is_active"])

    def test_http_get_blocking_reasons_uses_published_route(self) -> None:
        with self.server() as base_url:
            with urlopen(f"{base_url}/api/v1/blocking-reasons", timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))

            self.assertEqual(200, response.status)
            self.assertEqual("DESCRIPTION_MISMATCH", body[0]["code"])

    def test_legacy_product_blocking_reasons_route_is_not_mounted(self) -> None:
        with self.server() as base_url:
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{base_url}/api/v1/product-blocking-reasons", timeout=5)

            self.assertEqual(404, error.exception.code)
            try:
                body = json.loads(error.exception.read().decode("utf-8"))
            finally:
                error.exception.close()
            self.assertEqual({"code": "NOT_FOUND", "message": "Not found"}, body)

    def test_handler_accepts_reference_service_argument(self) -> None:
        handler = make_handler(
            product_event_service=None,
            b2b_to_mod_key="key",
            reference_service=self.service,
        )

        self.assertIsNotNone(handler)

    def server(self):
        test_case = self

        class ServerContext:
            def __enter__(self):
                handler = make_handler(
                    product_event_service=None,
                    b2b_to_mod_key="key",
                    reference_service=test_case.service,
                )
                self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                self.thread = Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()
                host, port = self.server.server_address
                return f"http://{host}:{port}"

            def __exit__(self, exc_type, exc, traceback):
                self.server.shutdown()
                self.thread.join(timeout=5)
                self.server.server_close()

        return ServerContext()


if __name__ == "__main__":
    unittest.main()
