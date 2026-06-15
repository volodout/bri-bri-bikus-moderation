from __future__ import annotations

import unittest
from unittest.mock import patch

from moderation.b2b_client import B2BClient


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class B2BClientTestCase(unittest.TestCase):
    def test_send_moderation_event_uses_b2b_contract_path(self) -> None:
        client = B2BClient("https://b2b.example.test", "service-key")

        with patch("moderation.b2b_client.urlopen", return_value=FakeResponse()) as urlopen:
            client.send_moderation_event(
                {
                    "idempotency_key": "11111111-1111-1111-1111-111111111111",
                    "product_id": "22222222-2222-2222-2222-222222222222",
                    "event_type": "MODERATED",
                    "occurred_at": "2026-03-15T14:30:00.000Z",
                }
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(
            "https://b2b.example.test/api/v1/moderation/events",
            request.full_url,
        )


if __name__ == "__main__":
    unittest.main()

