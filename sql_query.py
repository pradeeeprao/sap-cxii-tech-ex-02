"""Guarded NL-to-SQL orchestration with one explicit repair attempt."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm import LLMClient, LLMOutputError, LLMUnavailable

logger = logging.getLogger("orders.nl_sql")

SCHEMA_CONTEXT = """SQLite table: orders
- order_id TEXT PRIMARY KEY: unique order identifier
- customer_id TEXT NOT NULL: customer identifier
- order_date TEXT NOT NULL: normalized ISO-8601 calendar date (YYYY-MM-DD)
- amount_usd REAL NOT NULL: order amount converted to USD
"""

SQL_SYSTEM_PROMPT = """You translate user questions into safe SQLite SELECT queries.

Available database schema:
{schema}

Rules:
1. Use only the columns and table shown above.
2. Produce exactly one read-only SELECT statement (a WITH ... SELECT is allowed).
3. Never use INSERT, UPDATE, DELETE, DDL, PRAGMA, ATTACH, or comments.
4. For "recent" or "last N days", compare order_date with date('now', '-N days').
5. Use amount_usd for all money calculations and round monetary aggregates to 2 decimals.
6. If the question requires unavailable fields or cannot be answered from this schema,
   set answerable to false and explain the missing information.
7. Return JSON only, in exactly one of these shapes:
   {{"answerable": true, "sql": "SELECT ...", "reason": null}}
   {{"answerable": false, "sql": null, "reason": "..."}}
"""

ANSWER_SYSTEM_PROMPT = """You turn a SQL result into one short, factual answer.
Do not invent data. Amounts are USD. Return JSON only as {"answer": "..."}.
"""

DENIED_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|"
    r"vacuum|reindex|analyze|load_extension)\b",
    re.IGNORECASE,
)
ALLOWED_FUNCTIONS = {
    "abs",
    "avg",
    "coalesce",
    "count",
    "date",
    "datetime",
    "ifnull",
    "julianday",
    "length",
    "lower",
    "max",
    "min",
    "nullif",
    "printf",
    "round",
    "strftime",
    "substr",
    "sum",
    "total",
    "trim",
    "upper",
}


class UnsafeSQL(ValueError):
    pass


class QueryExecutionError(RuntimeError):
    pass


class UnanswerableQuestion(ValueError):
    pass


@dataclass(frozen=True)
class NLQueryResult:
    answer: str
    sql_used: str
    rows: list[dict[str, Any]]
    truncated: bool
    token_count: int


def clean_sql(sql: str) -> str:
    candidate = sql.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
        if candidate.lower().startswith("sql"):
            candidate = candidate[3:].lstrip()
    if candidate.endswith(";"):
        candidate = candidate[:-1].rstrip()
    if ";" in candidate:
        raise UnsafeSQL("multiple SQL statements are not allowed")
    if "--" in candidate or "/*" in candidate or "*/" in candidate:
        raise UnsafeSQL("SQL comments are not allowed")
    if not re.match(r"^(select|with)\b", candidate, flags=re.IGNORECASE):
        raise UnsafeSQL("only SELECT queries are allowed")
    match = DENIED_SQL.search(candidate)
    if match:
        raise UnsafeSQL(f"SQL keyword is not allowed: {match.group(1).upper()}")
    return candidate


def _authorizer(action: int, arg1: str | None, arg2: str | None, *_args) -> int:
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if arg1 == "orders" else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        function_name = (arg2 or arg1 or "").lower()
        return (
            sqlite3.SQLITE_OK
            if function_name in ALLOWED_FUNCTIONS
            else sqlite3.SQLITE_DENY
        )
    explicitly_denied = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_PRAGMA,
    }
    return sqlite3.SQLITE_DENY if action in explicitly_denied else sqlite3.SQLITE_OK


def execute_read_only(
    db_path: Path, sql: str, timeout_seconds: float, row_limit: int = 200
) -> tuple[list[dict[str, Any]], bool]:
    safe_sql = clean_sql(sql)
    deadline = time.monotonic() + timeout_seconds
    try:
        with sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=timeout_seconds
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.set_authorizer(_authorizer)
            connection.set_progress_handler(
                lambda: int(time.monotonic() > deadline), 1_000
            )
            cursor = connection.execute(safe_sql)
            result = cursor.fetchmany(row_limit + 1)
    except (sqlite3.Error, UnsafeSQL) as exc:
        raise QueryExecutionError(str(exc)) from exc
    truncated = len(result) > row_limit
    return [dict(row) for row in result[:row_limit]], truncated


class NLQueryService:
    def __init__(
        self,
        db_path: Path,
        llm: LLMClient,
        timeout_seconds: float = 2,
    ) -> None:
        self.db_path = db_path
        self.llm = llm
        self.timeout_seconds = timeout_seconds

    async def ask(self, question: str) -> NLQueryResult:
        request_id = str(uuid.uuid4())
        system_prompt = SQL_SYSTEM_PROMPT.format(schema=SCHEMA_CONTEXT.strip())
        error: str | None = None
        total_tokens = 0
        sql_used: str | None = None
        rows: list[dict[str, Any]] = []
        truncated = False

        # Initial generation plus exactly one repair generation on invalid output
        # or a SQLite runtime/safety error.
        for attempt in range(2):
            user_prompt = f"Question: {question}"
            if error:
                user_prompt += (
                    "\nThe previous SQL failed with this error: "
                    f"{error}\nReturn corrected SQL."
                )
            logger.info(
                "nl_sql_prompt request_id=%s attempt=%d prompt=%s",
                request_id,
                attempt + 1,
                json.dumps(
                    {"system": system_prompt, "user": user_prompt},
                    ensure_ascii=False,
                ),
            )
            try:
                proposal = await self.llm.propose_sql(system_prompt, user_prompt)
                total_tokens += proposal.token_count
                logger.info(
                    "nl_sql_generated request_id=%s attempt=%d sql=%s token_count=%d",
                    request_id,
                    attempt + 1,
                    json.dumps(proposal.sql),
                    proposal.token_count,
                )
                if not proposal.answerable:
                    raise UnanswerableQuestion(
                        proposal.reason
                        or "The question cannot be answered from the orders schema."
                    )
                sql_used = clean_sql(proposal.sql or "")
                rows, truncated = execute_read_only(
                    self.db_path, sql_used, self.timeout_seconds
                )
                break
            except UnanswerableQuestion:
                raise
            except LLMUnavailable:
                raise
            except (LLMOutputError, UnsafeSQL, QueryExecutionError) as exc:
                error = str(exc)
                logger.warning(
                    "nl_sql_attempt_failed request_id=%s attempt=%d error=%s",
                    request_id,
                    attempt + 1,
                    error,
                )
        else:
            raise UnanswerableQuestion(
                "The question could not be converted into a valid query after one retry: "
                f"{error}"
            )

        if sql_used is None:  # defensive; the loop either succeeds or raises
            raise UnanswerableQuestion("No SQL query was generated.")

        answer_prompt = (
            f"Question: {question}\nSQL: {sql_used}\n"
            f"Rows (JSON): {json.dumps(rows, default=str)}\n"
            f"Result truncated: {str(truncated).lower()}"
        )
        try:
            answer_result = await self.llm.summarize(
                ANSWER_SYSTEM_PROMPT, answer_prompt
            )
            total_tokens += answer_result.token_count
            answer = answer_result.answer
        except (LLMUnavailable, LLMOutputError) as exc:
            logger.warning(
                "nl_sql_summary_failed request_id=%s error=%s", request_id, exc
            )
            suffix = " (first 200 shown)" if truncated else ""
            answer = f"The query returned {len(rows)} row(s){suffix}."

        logger.info(
            "nl_sql_complete request_id=%s sql=%s token_count=%d rows=%d truncated=%s",
            request_id,
            json.dumps(sql_used),
            total_tokens,
            len(rows),
            truncated,
        )
        return NLQueryResult(
            answer=answer,
            sql_used=sql_used,
            rows=rows,
            truncated=truncated,
            token_count=total_tokens,
        )
