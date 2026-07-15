from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from etl import load
from llm import AnswerResult, SQLProposal
from sql_query import NLQueryService, UnanswerableQuestion, execute_read_only


class FakeLLM:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.prompts = []

    async def propose_sql(self, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        return self.proposals.pop(0)

    async def summarize(self, system_prompt, user_prompt):
        return AnswerResult("Total revenue: $11.00 (1 order)", 7)


def make_db(tmp_path: Path) -> Path:
    source = tmp_path / "orders.csv"
    source.write_text(
        "order_id,customer_id,order_date,amount,currency\n"
        "1,C1,2026-07-01,10,EUR\n",
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
    return db_path


def test_invalid_sql_retries_once_with_error(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            SQLProposal(True, "SELECT total FROM orders", None, 10, ""),
            SQLProposal(
                True,
                "SELECT ROUND(SUM(amount_usd), 2) AS total_revenue FROM orders",
                None,
                12,
                "",
            ),
        ]
    )
    result = asyncio.run(NLQueryService(make_db(tmp_path), llm).ask("revenue?"))
    assert result.rows == [{"total_revenue": 11.0}]
    assert result.token_count == 29
    assert "previous SQL failed" in llm.prompts[1]
    assert "no such column: total" in llm.prompts[1]


def test_unanswerable_question_is_rejected(tmp_path: Path) -> None:
    llm = FakeLLM(
        [SQLProposal(False, None, "Product category is not in the schema.", 5, "")]
    )
    with pytest.raises(UnanswerableQuestion, match="Product category"):
        asyncio.run(NLQueryService(make_db(tmp_path), llm).ask("top product?"))


def test_sql_sandbox_blocks_other_tables_and_functions(tmp_path: Path) -> None:
    db_path = make_db(tmp_path)
    with pytest.raises(Exception, match="prohibited"):
        execute_read_only(db_path, "SELECT * FROM etl_metadata", 1)
    with pytest.raises(Exception, match="not authorized"):
        execute_read_only(db_path, "SELECT randomblob(100000)", 1)
