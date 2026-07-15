"""CSV-to-SQLite ETL command for customer orders."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Iterator

from settings import Settings

logger = logging.getLogger("orders.etl")

REQUIRED_COLUMNS = {"order_id", "customer_id", "order_date", "amount", "currency"}
USD_RATES = {"USD": Decimal("1"), "EUR": Decimal("1.1")}
DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",  # explicitly allowed by the exercise
    "%d-%m-%Y",  # present in the supplied data
    "%m-%d-%Y",
    "%d/%m/%Y",
)


@dataclass
class LoadReport:
    extracted: int = 0
    loaded: int = 0
    rejected: int = 0
    invalid_amounts_defaulted: int = 0


@dataclass(frozen=True)
class CleanOrder:
    order_id: str
    customer_id: str
    order_date: str
    amount_usd: float


class RejectedRow(ValueError):
    pass


def normalize_date(value: str) -> str:
    value = value.strip()
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    raise RejectedRow(f"invalid order_date: {value or '<missing>'}")


def transform_row(row: dict[str, str | None]) -> tuple[CleanOrder, bool]:
    order_id = (row.get("order_id") or "").strip()
    customer_id = (row.get("customer_id") or "").strip()
    if not order_id:
        raise RejectedRow("missing order_id")
    if not customer_id:
        raise RejectedRow("missing customer_id")

    order_date = normalize_date(row.get("order_date") or "")
    currency = (row.get("currency") or "USD").strip().upper() or "USD"
    if currency not in USD_RATES:
        raise RejectedRow(f"unsupported currency: {currency}")

    raw_amount = (row.get("amount") or "").strip()
    amount_defaulted = False
    try:
        amount = Decimal(raw_amount) if raw_amount else Decimal("0")
        if not amount.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        amount = Decimal("0")
        amount_defaulted = True
    if not raw_amount:
        amount_defaulted = True

    amount_usd = (amount * USD_RATES[currency]).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return (
        CleanOrder(
            order_id=order_id,
            customer_id=customer_id,
            order_date=order_date,
            amount_usd=float(amount_usd),
        ),
        amount_defaulted,
    )


def resolve_csv_paths(inputs: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    for input_path in inputs:
        if input_path.is_dir():
            paths.extend(sorted(input_path.glob("*.csv")))
        elif input_path.is_file():
            paths.append(input_path)
        else:
            raise FileNotFoundError(f"input does not exist: {input_path}")
    if not paths:
        raise FileNotFoundError("no CSV files found")
    return paths


def read_csv(path: Path) -> Iterator[tuple[int, dict[str, str | None]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(
                f"{path} is missing required columns: {', '.join(sorted(missing))}"
            )
        for source_row, row in enumerate(reader, start=2):
            yield source_row, row


def _create_schema(connection: sqlite3.Connection, revision: str) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = FULL;

        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY NOT NULL,
            customer_id TEXT NOT NULL,
            order_date TEXT NOT NULL CHECK (order_date GLOB '????-??-??'),
            amount_usd REAL NOT NULL
        );

        CREATE INDEX idx_orders_customer_id ON orders(customer_id);
        CREATE INDEX idx_orders_order_date ON orders(order_date);

        CREATE TABLE etl_metadata (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        );

        CREATE TABLE etl_rejects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            source_row INTEGER NOT NULL,
            reason TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        """
    )
    connection.executemany(
        "INSERT INTO etl_metadata(key, value) VALUES (?, ?)",
        (
            ("revision", revision),
            ("loaded_at", datetime.now(timezone.utc).isoformat()),
            ("schema_version", "1"),
        ),
    )


def _populate_database(
    db_path: Path, csv_paths: list[Path], revision: str
) -> LoadReport:
    report = LoadReport()
    with sqlite3.connect(db_path) as connection:
        _create_schema(connection, revision)
        for csv_path in csv_paths:
            for source_row, row in read_csv(csv_path):
                report.extracted += 1
                try:
                    order, defaulted = transform_row(row)
                except RejectedRow as exc:
                    report.rejected += 1
                    connection.execute(
                        """
                        INSERT INTO etl_rejects(
                            source_file, source_row, reason, payload
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            str(csv_path),
                            source_row,
                            str(exc),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    continue
                report.invalid_amounts_defaulted += int(defaulted)
                connection.execute(
                    """
                    INSERT INTO orders(order_id, customer_id, order_date, amount_usd)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        customer_id = excluded.customer_id,
                        order_date = excluded.order_date,
                        amount_usd = excluded.amount_usd
                    """,
                    (
                        order.order_id,
                        order.customer_id,
                        order.order_date,
                        order.amount_usd,
                    ),
                )
        report.loaded = int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
        connection.executemany(
            "INSERT INTO etl_metadata(key, value) VALUES (?, ?)",
            (
                ("rows_extracted", str(report.extracted)),
                ("rows_loaded", str(report.loaded)),
                ("rows_rejected", str(report.rejected)),
                (
                    "invalid_amounts_defaulted",
                    str(report.invalid_amounts_defaulted),
                ),
            ),
        )
    return report


def load(
    inputs: Iterable[Path],
    db_path: Path,
    index_path: Path,
    embedding_model: str,
    embedding_cache_dir: Path | None = None,
    rebuild_semantic_index: bool = True,
) -> LoadReport:
    csv_paths = resolve_csv_paths(inputs)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    revision = str(uuid.uuid4())

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{db_path.name}.", suffix=".tmp", dir=db_path.parent
    )
    os.close(descriptor)
    temporary_db = Path(temporary_name)
    staged_index = index_path.with_name(f".{index_path.name}.{revision}.npz")
    try:
        report = _populate_database(temporary_db, csv_paths, revision)
        if rebuild_semantic_index:
            from semantic_index import SentenceTransformerEmbedder, rebuild_index

            embedder = SentenceTransformerEmbedder(
                embedding_model, cache_dir=embedding_cache_dir
            )
            rebuild_index(temporary_db, staged_index, embedder)

        os.replace(temporary_db, db_path)
        if rebuild_semantic_index:
            os.replace(staged_index, index_path)
    finally:
        temporary_db.unlink(missing_ok=True)
        staged_index.unlink(missing_ok=True)

    logger.info(
        "load complete extracted=%d loaded=%d rejected=%d amount_defaults=%d revision=%s",
        report.extracted,
        report.loaded,
        report.rejected,
        report.invalid_amounts_defaulted,
        revision,
    )
    return report


def show_stats(db_path: Path) -> dict[str, float | int]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(amount_usd), 0),
                   COALESCE(AVG(amount_usd), 0)
            FROM orders
            """
        ).fetchone()
    return {
        "order_count": int(row[0]),
        "total_revenue": round(float(row[1]), 2),
        "avg_order_value": round(float(row[2]), 2),
    }


def build_parser() -> argparse.ArgumentParser:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    load_parser = subcommands.add_parser("load", help="clean CSV files and load SQLite")
    load_parser.add_argument("inputs", type=Path, nargs="+")
    load_parser.add_argument("--db", type=Path, default=settings.db_path)
    load_parser.add_argument("--index", type=Path, default=settings.semantic_index_path)
    load_parser.add_argument("--embedding-model", default=settings.embedding_model)
    load_parser.add_argument(
        "--skip-index",
        action="store_true",
        help="development escape hatch; the API rebuilds a missing/stale index",
    )

    stats_parser = subcommands.add_parser("show-stats", help="print aggregate stats")
    stats_parser.add_argument("--db", type=Path, default=settings.db_path)
    return parser


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args(argv)
    try:
        if args.command == "load":
            report = load(
                args.inputs,
                args.db.resolve(),
                args.index.resolve(),
                args.embedding_model,
                settings.embedding_cache_dir,
                not args.skip_index,
            )
            print(json.dumps(report.__dict__, sort_keys=True))
        else:
            print(json.dumps(show_stats(args.db.resolve()), sort_keys=True))
    except (FileNotFoundError, ValueError, RuntimeError, sqlite3.Error) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
