"""Versioned in-memory cosine index for order semantic search."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """Minimal adapter used by both ETL and the API."""

    model_name: str

    def encode(self, texts: Sequence[str]) -> np.ndarray: ...


class SentenceTransformerEmbedder:
    """Lazy sentence-transformers adapter with normalized output vectors."""

    def __init__(self, model_name: str, cache_dir: Path | None = None) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model = None
        self._lock = threading.Lock()

    def _get_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    logger.info("loading embedding model model=%s", self.model_name)
                    self._model = SentenceTransformer(
                        self.model_name,
                        cache_folder=str(self.cache_dir) if self.cache_dir else None,
                    )
        return self._model

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        vectors = self._get_model().encode(
            list(texts),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


@dataclass(frozen=True)
class IndexSnapshot:
    revision: str
    model_name: str
    order_ids: np.ndarray
    customer_ids: np.ndarray
    order_dates: np.ndarray
    amounts_usd: np.ndarray
    vectors: np.ndarray


@dataclass(frozen=True)
class SemanticMatch:
    order_id: str
    customer_id: str
    order_date: str
    amount_usd: float
    score: float


def order_to_text(
    order_id: str, customer_id: str, order_date: str, amount_usd: float
) -> str:
    return (
        f"order {order_id}; customer {customer_id}; "
        f"amount {amount_usd:.2f} USD; order date {order_date}"
    )


def _read_orders(db_path: Path) -> tuple[str, list[sqlite3.Row]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        revision_row = connection.execute(
            "SELECT value FROM etl_metadata WHERE key = 'revision'"
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("database has no ETL revision")
        rows = connection.execute(
            """
            SELECT order_id, customer_id, order_date, amount_usd
            FROM orders
            ORDER BY order_id
            """
        ).fetchall()
    return str(revision_row[0]), rows


def database_revision(db_path: Path) -> str:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT value FROM etl_metadata WHERE key = 'revision'"
        ).fetchone()
    if row is None:
        raise RuntimeError("database has no ETL revision")
    return str(row[0])


def build_snapshot(db_path: Path, embedder: Embedder) -> IndexSnapshot:
    revision, rows = _read_orders(db_path)
    texts = [
        order_to_text(
            str(row["order_id"]),
            str(row["customer_id"]),
            str(row["order_date"]),
            float(row["amount_usd"]),
        )
        for row in rows
    ]
    vectors = embedder.encode(texts)
    if len(rows) != len(vectors):
        raise RuntimeError("embedding model returned the wrong number of vectors")
    order_ids = np.asarray([str(row["order_id"]) for row in rows], dtype=np.str_)
    return IndexSnapshot(
        revision=revision,
        model_name=embedder.model_name,
        order_ids=order_ids,
        customer_ids=np.asarray(
            [str(row["customer_id"]) for row in rows], dtype=np.str_
        ),
        order_dates=np.asarray(
            [str(row["order_date"]) for row in rows], dtype=np.str_
        ),
        amounts_usd=np.asarray(
            [float(row["amount_usd"]) for row in rows], dtype=np.float64
        ),
        vectors=vectors,
    )


def write_snapshot(snapshot: IndexSnapshot, index_path: Path) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{index_path.name}.", suffix=".npz", dir=index_path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        np.savez_compressed(
            temporary_path,
            revision=np.asarray(snapshot.revision),
            model_name=np.asarray(snapshot.model_name),
            order_ids=snapshot.order_ids,
            customer_ids=snapshot.customer_ids,
            order_dates=snapshot.order_dates,
            amounts_usd=snapshot.amounts_usd,
            vectors=snapshot.vectors,
        )
        os.replace(temporary_path, index_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def rebuild_index(
    db_path: Path, index_path: Path, embedder: Embedder
) -> IndexSnapshot:
    snapshot = build_snapshot(db_path, embedder)
    write_snapshot(snapshot, index_path)
    logger.info(
        "semantic index rebuilt revision=%s records=%d path=%s",
        snapshot.revision,
        len(snapshot.order_ids),
        index_path,
    )
    return snapshot


def load_snapshot(index_path: Path) -> IndexSnapshot:
    with np.load(index_path, allow_pickle=False) as contents:
        snapshot = IndexSnapshot(
            revision=str(contents["revision"].item()),
            model_name=str(contents["model_name"].item()),
            order_ids=np.array(contents["order_ids"], copy=True),
            customer_ids=np.array(contents["customer_ids"], copy=True),
            order_dates=np.array(contents["order_dates"], copy=True),
            amounts_usd=np.asarray(contents["amounts_usd"], dtype=np.float64).copy(),
            vectors=np.asarray(contents["vectors"], dtype=np.float32).copy(),
        )
    if snapshot.vectors.ndim != 2:
        raise ValueError("semantic index vectors must be a matrix")
    lengths = {
        len(snapshot.order_ids),
        len(snapshot.customer_ids),
        len(snapshot.order_dates),
        len(snapshot.amounts_usd),
        len(snapshot.vectors),
    }
    if len(lengths) != 1:
        raise ValueError("semantic index metadata and vectors have different lengths")
    return snapshot


class SemanticIndexManager:
    """Loads and atomically swaps immutable index snapshots.

    A background task notices ETL revision changes. Expensive model inference and
    disk work happen in a worker thread; the event loop and current snapshot stay
    available throughout a rebuild.
    """

    def __init__(
        self,
        db_path: Path,
        index_path: Path,
        embedder: Embedder,
        poll_seconds: float = 5.0,
    ) -> None:
        self.db_path = db_path
        self.index_path = index_path
        self.embedder = embedder
        self.poll_seconds = poll_seconds
        self._snapshot: IndexSnapshot | None = None
        self._refresh_lock = threading.Lock()
        self._refresh_thread: threading.Thread | None = None
        self._watch_task: asyncio.Task | None = None
        self.last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self._snapshot is not None

    @property
    def revision(self) -> str | None:
        return self._snapshot.revision if self._snapshot else None

    async def start(self) -> None:
        # Startup intentionally waits for one valid snapshot before readiness.
        # Later refreshes use a background thread and never replace the active
        # snapshot until a complete candidate is available.
        self._refresh(True)
        self._watch_task = asyncio.create_task(
            self._watch(), name="semantic-index-watcher"
        )

    async def stop(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            self._start_background_refresh()

    def _start_background_refresh(self) -> None:
        if self._refresh_thread and self._refresh_thread.is_alive():
            return

        def refresh_safely() -> None:
            try:
                self._refresh(False)
            except Exception as exc:  # keep serving the previous valid snapshot
                self.last_error = str(exc)
                logger.exception("semantic index refresh failed")

        self._refresh_thread = threading.Thread(
            target=refresh_safely,
            daemon=True,
            name="semantic-index-refresh",
        )
        self._refresh_thread.start()

    def _refresh(self, force: bool) -> None:
        with self._refresh_lock:
            revision = database_revision(self.db_path)
            current = self._snapshot
            if not force and current and current.revision == revision:
                return

            try:
                candidate = load_snapshot(self.index_path)
            except (FileNotFoundError, KeyError, OSError, ValueError):
                candidate = None

            if (
                candidate is None
                or candidate.revision != revision
                or candidate.model_name != self.embedder.model_name
            ):
                candidate = rebuild_index(
                    self.db_path, self.index_path, self.embedder
                )

            self._snapshot = candidate
            self.last_error = None
            logger.info(
                "semantic index activated revision=%s records=%d",
                candidate.revision,
                len(candidate.order_ids),
            )

    async def search(self, query: str, top_k: int) -> list[SemanticMatch]:
        return self._search(query, top_k)

    def _search(self, query: str, top_k: int) -> list[SemanticMatch]:
        snapshot = self._snapshot
        if snapshot is None:
            raise RuntimeError("semantic index is not ready")
        if len(snapshot.order_ids) == 0:
            return []
        query_vector = self.embedder.encode([query])
        if query_vector.shape[1] != snapshot.vectors.shape[1]:
            raise RuntimeError("query and index embedding dimensions differ")
        scores = snapshot.vectors @ query_vector[0]
        count = min(top_k, len(scores))
        indices = np.argpartition(-scores, count - 1)[:count]
        indices = indices[np.argsort(-scores[indices])]
        return [
            SemanticMatch(
                order_id=str(snapshot.order_ids[index]),
                customer_id=str(snapshot.customer_ids[index]),
                order_date=str(snapshot.order_dates[index]),
                amount_usd=float(snapshot.amounts_usd[index]),
                score=float(scores[index]),
            )
            for index in indices
        ]
