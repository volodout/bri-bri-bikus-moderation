from __future__ import annotations

import tempfile
import unittest

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
                "title": "Описание не соответствует товару",
                "hard_block": False,
            },
            reasons[0],
        )
        self.assertEqual(
            {
                "id": "d6e7f8a9-0123-4567-7890-789012345678",
                "title": "Товар нарушает авторские права",
                "hard_block": True,
            },
            reasons[-1],
        )

    def test_handler_accepts_reference_service_argument(self) -> None:
        handler = make_handler(
            product_event_service=None,
            b2b_to_mod_key="key",
            reference_service=self.service,
        )

        self.assertIsNotNone(handler)


if __name__ == "__main__":
    unittest.main()

