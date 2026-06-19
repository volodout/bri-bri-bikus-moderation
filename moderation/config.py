"""Runtime configuration for the moderation service."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    b2b_to_mod_key: str
    mod_to_b2b_key: str
    b2b_base_url: str
    database_path: str
    database_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            b2b_to_mod_key=os.environ.get("B2B_TO_MOD_KEY", "dev-b2b-to-mod"),
            mod_to_b2b_key=os.environ.get("MOD_TO_B2B_KEY", "dev-mod-to-b2b"),
            b2b_base_url=os.environ.get("B2B_BASE_URL", ""),
            database_path=os.environ.get("MODERATION_DB_PATH", "moderation.sqlite3"),
            database_url=os.environ.get("MODERATION_DATABASE_URL", ""),
            host=os.environ.get("MODERATION_HOST", "127.0.0.1"),
            port=int(os.environ.get("MODERATION_PORT", "8000")),
        )
