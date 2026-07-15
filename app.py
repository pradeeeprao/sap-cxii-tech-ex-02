"""FastAPI query service for cleaned customer orders."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from llm import LLMClient, LLMUnavailable, OllamaClient
from semantic_index import SemanticIndexManager, SentenceTransformerEmbedder
from settings import Settings
from sql_query import NLQueryService, UnanswerableQuestion

logger = logging.getLogger("orders.api")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1_000)


class AskResponse(BaseModel):
    answer: str
    sql_used: str
    rows: list[dict[str, Any]]
    truncated: bool = False


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def create_app(
    settings: Settings | None = None,
    *,
    llm_client: LLMClient | None = None,
    index_manager: SemanticIndexManager | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    llm_client = llm_client or OllamaClient(
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_timeout_seconds,
    )
    if index_manager is None:
        embedder = SentenceTransformerEmbedder(
            settings.embedding_model, settings.embedding_cache_dir
        )
        index_manager = SemanticIndexManager(
            settings.db_path,
            settings.semantic_index_path,
            embedder,
            settings.index_poll_seconds,
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if not settings.db_path.is_file():
            raise RuntimeError(
                f"orders database not found at {settings.db_path}; run etl.py load first"
            )
        await index_manager.start()
        application.state.index_manager = index_manager
        application.state.nl_query = NLQueryService(
            settings.db_path, llm_client, settings.sql_timeout_seconds
        )
        try:
            yield
        finally:
            await index_manager.stop()

    application = FastAPI(
        title="Customer Orders API",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.get("/healthz", tags=["operations"])
    async def healthz() -> str:
        return "ok"

    @application.get("/readyz", tags=["operations"])
    async def readyz(request: Request) -> dict[str, str]:
        manager: SemanticIndexManager = request.app.state.index_manager
        if not settings.db_path.is_file() or not manager.ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="database or semantic index is not ready",
            )
        return {"status": "ready", "index_revision": manager.revision or "unknown"}

    @application.get("/orders/customer/{customer_id}", tags=["orders"])
    async def customer_orders(customer_id: str) -> list[dict[str, Any]]:
        with _connect(settings.db_path) as connection:
            rows = connection.execute(
                """
                SELECT order_id, customer_id, order_date, amount_usd
                FROM orders WHERE customer_id = ?
                ORDER BY order_date DESC, order_id
                """,
                (customer_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @application.get("/orders/stats", tags=["orders"])
    async def order_stats() -> dict[str, Any]:
        with _connect(settings.db_path) as connection:
            aggregate = connection.execute(
                """
                SELECT COALESCE(SUM(amount_usd), 0) AS total_revenue,
                       COALESCE(AVG(amount_usd), 0) AS avg_order_value
                FROM orders
                """
            ).fetchone()
            daily = connection.execute(
                """
                SELECT order_date, COUNT(*) AS order_count
                FROM orders GROUP BY order_date ORDER BY order_date
                """
            ).fetchall()
        return {
            "total_revenue": round(float(aggregate["total_revenue"]), 2),
            "avg_order_value": round(float(aggregate["avg_order_value"]), 2),
            "orders_per_day": {
                str(row["order_date"]): int(row["order_count"]) for row in daily
            },
        }

    @application.get("/orders/recent", tags=["orders"])
    async def recent_orders(
        days: int = Query(default=30, ge=1, le=36_500)
    ) -> list[dict[str, Any]]:
        with _connect(settings.db_path) as connection:
            rows = connection.execute(
                """
                SELECT order_id, customer_id, order_date, amount_usd
                FROM orders
                WHERE order_date >= date('now', ?)
                  AND order_date <= date('now')
                ORDER BY order_date DESC, order_id
                """,
                (f"-{days} days",),
            ).fetchall()
        return [dict(row) for row in rows]

    @application.post("/orders/ask", response_model=AskResponse, tags=["ai"])
    async def ask_orders(payload: AskRequest, request: Request) -> AskResponse:
        service: NLQueryService = request.app.state.nl_query
        try:
            result = await service.ask(payload.question)
        except UnanswerableQuestion as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LLMUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="Natural-language query provider is unavailable."
            ) from exc
        return AskResponse(
            answer=result.answer,
            sql_used=result.sql_used,
            rows=result.rows,
            truncated=result.truncated,
        )

    @application.get("/orders/semantic_search", tags=["ai"])
    async def semantic_search(
        request: Request,
        q: str = Query(min_length=2, max_length=500),
        top_k: int = Query(default=5, ge=1, le=50),
    ) -> list[dict[str, Any]]:
        manager: SemanticIndexManager = request.app.state.index_manager
        try:
            matches = await manager.search(q, top_k)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return [
            {
                "order_id": match.order_id,
                "customer_id": match.customer_id,
                "amount_usd": match.amount_usd,
                "order_date": match.order_date,
                "score": round(match.score, 6),
            }
            for match in matches
        ]

    return application


logging.basicConfig(
    level=getattr(logging, Settings.from_env().log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
app = create_app()
