"""Environment-backed application settings.

Keeping configuration in one small module makes the API, ETL command, tests, and
container manifests agree on paths and model names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path_env(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    db_path: Path
    semantic_index_path: Path
    embedding_model: str
    embedding_cache_dir: Path | None
    index_poll_seconds: float
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: float
    sql_timeout_seconds: float
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        cache = os.getenv("EMBEDDING_CACHE_DIR")
        return cls(
            db_path=_path_env("DB_PATH", "data/orders.db"),
            semantic_index_path=_path_env(
                "SEMANTIC_INDEX_PATH", "data/orders-index.npz"
            ),
            embedding_model=os.getenv(
                "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
            ),
            embedding_cache_dir=Path(cache).resolve() if cache else None,
            index_poll_seconds=float(os.getenv("INDEX_POLL_SECONDS", "5")),
            llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434").rstrip(
                "/"
            ),
            llm_model=os.getenv("LLM_MODEL", "llama3.1:8b"),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "45")),
            sql_timeout_seconds=float(os.getenv("SQL_TIMEOUT_SECONDS", "2")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
