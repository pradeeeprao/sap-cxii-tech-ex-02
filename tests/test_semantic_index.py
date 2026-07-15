from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np

from etl import load
from semantic_index import SemanticIndexManager, rebuild_index


class KeywordEmbedder:
    model_name = "keyword-test-model"

    def encode(self, texts):
        vectors = []
        for text in texts:
            lower = text.lower()
            vector = np.asarray(
                [
                    float("c1" in lower),
                    float("1000" in lower or "high" in lower),
                    float("recent" in lower or "2026" in lower),
                ],
                dtype=np.float32,
            )
            norm = np.linalg.norm(vector)
            vectors.append(vector / norm if norm else vector)
        return np.stack(vectors)


def test_index_build_load_and_search(tmp_path: Path) -> None:
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "order_id,customer_id,order_date,amount,currency\n"
        "1,C1,2026-07-01,1000,USD\n"
        "2,C2,2024-01-01,10,USD\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"
    index_path = tmp_path / "orders.npz"
    load(
        [csv_path],
        db_path,
        index_path,
        "ignored",
        rebuild_semantic_index=False,
    )
    embedder = KeywordEmbedder()
    rebuild_index(db_path, index_path, embedder)
    manager = SemanticIndexManager(db_path, index_path, embedder, poll_seconds=60)

    async def scenario():
        await manager.start()
        try:
            return await manager.search("high value recent C1", 1)
        finally:
            await manager.stop()

    assert asyncio.run(scenario())[0].order_id == "1"
