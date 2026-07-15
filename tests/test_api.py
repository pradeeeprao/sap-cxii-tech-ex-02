from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app import create_app
from etl import load
from llm import AnswerResult, SQLProposal
from semantic_index import SemanticMatch
from settings import Settings


class FakeLLM:
    async def propose_sql(self, system_prompt, user_prompt):
        return SQLProposal(
            True,
            "SELECT SUM(amount_usd) AS total_revenue FROM orders",
            None,
            4,
            "",
        )

    async def summarize(self, system_prompt, user_prompt):
        return AnswerResult("Total revenue: $31.00", 3)


class FakeIndexManager:
    ready = True
    revision = "test"

    async def start(self):
        return None

    async def stop(self):
        return None

    async def search(self, query, top_k):
        return [SemanticMatch("1", "C1", "2026-07-01", 11.0, 0.91)][:top_k]


def settings_for(db_path: Path, tmp_path: Path) -> Settings:
    return Settings(
        db_path=db_path,
        semantic_index_path=tmp_path / "index.npz",
        embedding_model="test",
        embedding_cache_dir=None,
        index_poll_seconds=60,
        llm_base_url="http://unused",
        llm_model="test",
        llm_timeout_seconds=1,
        sql_timeout_seconds=1,
        log_level="INFO",
    )


def test_api_endpoints(tmp_path: Path) -> None:
    source = tmp_path / "orders.csv"
    source.write_text(
        "order_id,customer_id,order_date,amount,currency\n"
        "1,C1,2026-07-01,10,EUR\n"
        "2,C1,2026-07-02,20,USD\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"
    load(
        [source],
        db_path,
        tmp_path / "index.npz",
        "test",
        rebuild_semantic_index=False,
    )
    application = create_app(
        settings_for(db_path, tmp_path),
        llm_client=FakeLLM(),
        index_manager=FakeIndexManager(),
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=application)
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                assert (await client.get("/healthz")).json() == "ok"
                assert len((await client.get("/orders/customer/C1")).json()) == 2
                stats = await client.get("/orders/stats")
                assert stats.json()["total_revenue"] == 31.0
                ask = await client.post(
                    "/orders/ask", json={"question": "total revenue"}
                )
                assert ask.status_code == 200
                assert ask.json()["sql_used"].startswith("SELECT SUM")
                semantic = await client.get(
                    "/orders/semantic_search",
                    params={"q": "high value", "top_k": 1},
                )
                assert semantic.status_code == 200
                assert semantic.json()[0]["score"] == 0.91

    asyncio.run(scenario())
