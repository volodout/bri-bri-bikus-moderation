"""Reference endpoints for moderation dictionaries."""

from __future__ import annotations

from typing import Any

from moderation.database import BLOCKING_REASON_SEEDS, ModerationStore


class ReferenceService:
    def __init__(self, store: ModerationStore):
        self.store = store

    def blocking_reasons(self) -> list[dict[str, Any]]:
        self.store.ensure_schema()
        connection = self.store.connect()
        try:
            rows = connection.execute(
                """
                SELECT id, code, title, hard_block, is_active
                FROM product_blocking_reasons
                WHERE is_active = 1
                """
            ).fetchall()
        finally:
            connection.close()

        by_id = {row["id"]: row for row in rows}
        ordered_reasons = []
        for reason_id, _, _, _ in BLOCKING_REASON_SEEDS:
            row = by_id.pop(reason_id, None)
            if row is not None:
                ordered_reasons.append(_reason_to_json(row))

        for row in sorted(by_id.values(), key=lambda item: item["title"]):
            ordered_reasons.append(_reason_to_json(row))

        return ordered_reasons


def _reason_to_json(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "title": row["title"],
        "hard_block": bool(row["hard_block"]),
        "is_active": bool(row["is_active"]),
    }
