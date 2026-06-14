"""Reference endpoints for moderation dictionaries."""

from __future__ import annotations

from typing import Any

from moderation.database import BLOCKING_REASON_SEEDS, ModerationStore


class ReferenceService:
    def __init__(self, store: ModerationStore):
        self.store = store

    def blocking_reasons(self) -> list[dict[str, Any]]:
        self.store.ensure_schema()
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, hard_block
                FROM product_blocking_reasons
                """
            ).fetchall()

        by_id = {row["id"]: row for row in rows}
        ordered_reasons = []
        for reason_id, _, _ in BLOCKING_REASON_SEEDS:
            row = by_id.pop(reason_id, None)
            if row is not None:
                ordered_reasons.append(_reason_to_json(row))

        for row in sorted(by_id.values(), key=lambda item: item["title"]):
            ordered_reasons.append(_reason_to_json(row))

        return ordered_reasons


def _reason_to_json(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "hard_block": bool(row["hard_block"]),
    }

