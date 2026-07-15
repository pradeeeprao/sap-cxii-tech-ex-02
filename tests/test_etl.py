from __future__ import annotations

import sqlite3
from pathlib import Path

from etl import load, normalize_date, transform_row


def test_transform_normalizes_and_converts() -> None:
    order, defaulted = transform_row(
        {
            "order_id": "1001",
            "customer_id": "C001",
            "order_date": "12/31/2024",
            "amount": "12.345",
            "currency": "EUR",
        }
    )
    assert order.order_date == "2024-12-31"
    assert order.amount_usd == 13.58
    assert defaulted is False
    assert normalize_date("31-12-2024") == "2024-12-31"


def test_load_defaults_bad_amount_and_quarantines_bad_keys(tmp_path: Path) -> None:
    source = tmp_path / "orders.csv"
    source.write_text(
        "order_id,customer_id,order_date,amount,currency\n"
        "1,C1,2024-01-02,10,EUR\n"
        "2,C2,01/03/2024,N/A,\n"
        ",C3,2024-01-04,20,USD\n"
        "4,,2024-01-05,20,USD\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"
    report = load(
        [source],
        db_path,
        tmp_path / "index.npz",
        "test-model",
        rebuild_semantic_index=False,
    )

    assert report.extracted == 4
    assert report.loaded == 2
    assert report.rejected == 2
    assert report.invalid_amounts_defaulted == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT order_date, amount_usd FROM orders WHERE order_id = '1'"
        ).fetchone() == ("2024-01-02", 11.0)
        assert connection.execute(
            "SELECT amount_usd FROM orders WHERE order_id = '2'"
        ).fetchone() == (0.0,)
        assert connection.execute("SELECT COUNT(*) FROM etl_rejects").fetchone() == (
            2,
        )
