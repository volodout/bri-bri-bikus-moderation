"""PostgreSQL persistence for production moderation flows."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from moderation.database import BLOCKING_REASON_SEEDS
from moderation.errors import ConflictError

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised only in PostgreSQL deployments.
    psycopg = None
    dict_row = None


class PostgresConnection:
    def __init__(self, connection: Any):
        self.connection = connection

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.connection.close()

    def execute(self, query: str, params: Any = ()) -> Any:
        return self.connection.execute(_translate_sql(query), params)

    def executemany(self, query: str, params_seq: list[tuple[Any, ...]]) -> None:
        translated_query = _translate_sql(query)
        with self.connection.cursor() as cursor:
            cursor.executemany(translated_query, params_seq)

    def close(self) -> None:
        self.connection.close()


class PostgresModerationStore:
    def __init__(self, database_url: str):
        if psycopg is None:
            raise RuntimeError(
                "PostgreSQL support requires psycopg. Install psycopg[binary] "
                "or use MODERATION_DB_PATH for local SQLite."
            )
        self.database_url = database_url

    def connect(self) -> PostgresConnection:
        return PostgresConnection(
            psycopg.connect(self.database_url, row_factory=dict_row)
        )

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[PostgresConnection]:
        del immediate
        connection = self.connect()
        try:
            yield connection
            connection.connection.commit()
        except Exception:
            connection.connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS product_moderation (
                    id UUID PRIMARY KEY,
                    product_id UUID NOT NULL UNIQUE,
                    seller_id UUID NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('PENDING', 'IN_REVIEW', 'APPROVED', 'BLOCKED', 'HARD_BLOCKED')
                    ),
                    queue_priority INTEGER NOT NULL CHECK (queue_priority BETWEEN 1 AND 4),
                    json_before TEXT,
                    json_after TEXT NOT NULL,
                    blocking_reason_id UUID,
                    moderator_id UUID,
                    moderator_comment TEXT,
                    total_active_quantity INTEGER NOT NULL DEFAULT 0,
                    date_created TEXT NOT NULL,
                    date_updated TEXT NOT NULL,
                    date_moderation TEXT,
                    last_event_date TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS product_moderation_field_report (
                    id UUID PRIMARY KEY,
                    product_moderation_id UUID NOT NULL REFERENCES product_moderation(id) ON DELETE CASCADE,
                    field_path TEXT NOT NULL,
                    sku_id UUID,
                    message TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'ERROR' CHECK (
                        severity IN ('INFO', 'WARNING', 'ERROR')
                    ),
                    date_created TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS product_blocking_reasons (
                    id UUID PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    hard_block INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_events (
                    id UUID PRIMARY KEY,
                    sender_service TEXT NOT NULL,
                    idempotency_key UUID NOT NULL,
                    response_cached TEXT,
                    processed_at TEXT NOT NULL,
                    UNIQUE(sender_service, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_product_moderation_queue
                    ON product_moderation(status, queue_priority, date_updated)
                """
            )
            for reason_id, code, title, hard_block in BLOCKING_REASON_SEEDS:
                connection.execute(
                    """
                    INSERT INTO product_blocking_reasons (
                        id,
                        code,
                        title,
                        hard_block,
                        is_active
                    )
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT (id) DO UPDATE
                    SET code = COALESCE(product_blocking_reasons.code, EXCLUDED.code),
                        title = EXCLUDED.title,
                        hard_block = EXCLUDED.hard_block
                    """,
                    (reason_id, code, title, hard_block),
                )

    def claim_next_pending_card(
        self,
        queue_ids: list[int],
        moderator_id: str,
        now: str,
    ) -> Any | None:
        with self.transaction() as connection:
            held = connection.execute(
                """
                SELECT 1
                FROM product_moderation
                WHERE status = 'IN_REVIEW'
                  AND moderator_id = ?
                LIMIT 1
                """,
                (moderator_id,),
            ).fetchone()
            if held is not None:
                raise ConflictError("Moderator already has a ticket in review")

            for queue_id in queue_ids:
                row = connection.execute(
                    """
                    SELECT *
                    FROM product_moderation
                    WHERE status = 'PENDING'
                      AND queue_priority = ?
                    ORDER BY date_updated ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    (queue_id,),
                ).fetchone()
                if row is None:
                    continue

                connection.execute(
                    """
                    UPDATE product_moderation
                    SET status = 'IN_REVIEW',
                        moderator_id = ?,
                        date_updated = ?
                    WHERE id = ?
                    """,
                    (moderator_id, now, row["id"]),
                )
                return connection.execute(
                    """
                    SELECT *
                    FROM product_moderation
                    WHERE id = ?
                    """,
                    (row["id"],),
                ).fetchone()

        return None


def _translate_sql(query: str) -> str:
    query = query.replace("?", "%s")
    if "INSERT OR IGNORE INTO processed_events" in query:
        query = query.replace(
            "INSERT OR IGNORE INTO processed_events",
            "INSERT INTO processed_events",
        )
        query = f"{query} ON CONFLICT (sender_service, idempotency_key) DO NOTHING"
    return query
