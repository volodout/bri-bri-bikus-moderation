"""SQLite persistence for moderation flows.

The schema mirrors the canonical PostgreSQL tables closely enough for local
development and tests. UUIDs and JSON are stored as text.
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Iterator


class ModerationStore:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS product_moderation (
                    id TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL UNIQUE,
                    seller_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('PENDING', 'IN_REVIEW', 'MODERATED', 'BLOCKED', 'HARD_BLOCKED')
                    ),
                    queue_priority INTEGER NOT NULL CHECK (queue_priority BETWEEN 1 AND 4),
                    json_before TEXT,
                    json_after TEXT NOT NULL,
                    blocking_reason_id TEXT,
                    moderator_id TEXT,
                    moderator_comment TEXT,
                    total_active_quantity INTEGER NOT NULL DEFAULT 0,
                    date_created TEXT NOT NULL,
                    date_updated TEXT NOT NULL,
                    date_moderation TEXT,
                    last_event_date TEXT
                );

                CREATE TABLE IF NOT EXISTS product_moderation_field_report (
                    id TEXT PRIMARY KEY,
                    product_moderation_id TEXT NOT NULL,
                    field_name TEXT NOT NULL CHECK (
                        field_name IN (
                            'title',
                            'description',
                            'product_images',
                            'category',
                            'sku_name',
                            'sku_image',
                            'sku_price'
                        )
                    ),
                    sku_id TEXT,
                    comment TEXT NOT NULL,
                    date_created TEXT NOT NULL,
                    FOREIGN KEY(product_moderation_id)
                        REFERENCES product_moderation(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS product_blocking_reasons (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    hard_block INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS processed_events (
                    id TEXT PRIMARY KEY,
                    sender_service TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    response_cached TEXT,
                    processed_at TEXT NOT NULL,
                    UNIQUE(sender_service, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_product_moderation_queue
                    ON product_moderation(status, queue_priority, date_updated);
                """
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO product_blocking_reasons (id, title, hard_block)
                VALUES (?, ?, ?)
                """,
                [
                    ("a7b8c9d0-1234-5678-ef01-890123456789", "Описание не соответствует товару", 0),
                    ("b8c9d0e1-2345-6789-f012-901234567890", "Изображение не соответствует товару", 0),
                    ("c9d0e1f2-3456-7890-0123-012345678901", "Некорректная категория товара", 0),
                    ("d0e1f2a3-4567-8901-1234-123456789012", "Недостаточно информации о товаре", 0),
                    ("e1f2a3b4-5678-9012-2345-234567890123", "Нецензурные или оскорбительные материалы", 0),
                    ("f2a3b4c5-6789-0123-3456-345678901234", "Дублирование существующего товара", 0),
                    ("a3b4c5d6-7890-1234-4567-456789012345", "Некорректная цена", 0),
                    ("b4c5d6e7-8901-2345-5678-567890123456", "Контрафактный товар", 1),
                    ("c5d6e7f8-9012-3456-6789-678901234567", "Товар запрещён к продаже на территории РФ", 1),
                    ("d6e7f8a9-0123-4567-7890-789012345678", "Товар нарушает авторские права", 1),
                ],
            )
