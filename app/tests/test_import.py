import io
import zipfile
from datetime import date
from xml.sax.saxutils import escape

import pytest

from finance import importer
from finance.db import get_db

NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
)


def cell(value):
    if value is None:
        return "<table:table-cell/>"
    if isinstance(value, bool):
        return f'<table:table-cell office:value-type="boolean" office:boolean-value="{str(value).lower()}"/>'
    if isinstance(value, (int, float)):
        return f'<table:table-cell office:value-type="float" office:value="{value}"/>'
    if isinstance(value, str) and value.startswith("D:"):
        return f'<table:table-cell office:value-type="date" office:date-value="{value[2:]}"/>'
    return f'<table:table-cell office:value-type="string"><text:p>{escape(value)}</text:p></table:table-cell>'


def ods(sheets):
    tables = "".join(
        f'<table:table table:name="{name}">'
        + "".join("<table:table-row>" + "".join(cell(v) for v in row) + "</table:table-row>" for row in rows)
        + '<table:table-row table:number-rows-repeated="1000"><table:table-cell table:number-columns-repeated="1024"/></table:table-row>'
        + "</table:table>"
        for name, rows in sheets.items()
    )
    content = f'<?xml version="1.0"?><office:document-content {NS}><office:body><office:spreadsheet>{tables}</office:spreadsheet></office:body></office:document-content>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", content)
    buffer.seek(0)
    return buffer


WORKBOOK = {
    "WebHookData": [
        ["date time", "User", "Price", "Category", "Comment", "Income true false"],
        ["D:2026-01-04T09:09:12.593", "Ben N", 6.04, "Food", "hoferic", 0.0],
        ["D:2026-01-05T13:11:01", "undefined undefined", 65.51, "Gas", None, 0.0],
        ["D:2026-01-10T03:14:41"],  # a cleared repeat row the old trigger kept logging
        ["D:2026-01-20T10:00:00", "Ana Novak", 2500.0, "placa", "salary", 1.0],
        ["D:2026-02-01T10:00:00", "Ana Novak", -7.86, "Bike", "refund", 0.0],
        ["D:2026-02-02T10:00:00", "Ben", 12.0, "Clotes", 5.0, False],
    ],
    "repeatData": [
        ["Date Time", "User", "Price", "Category", "Comment", "Income", "dayOfLogging"],
        ["D:2024-05-28T17:59:53", "Ana Novak", 57.21, "insurance", "life insurance", 0.0, 18.0],
        [None, None, None, None, None, None, 10.0],
    ],
    "montlyBudget": [
        ["Category", "Budget", "Type"],
        ["Food", 500.0, "Monthly", 500.0],
        ["Car", 1200.0, "Yearly", 100.0],
        ["Kite", 0.0, "Yearly", 0.0],
        ["Tech"],
    ],
    "Categories": [
        ["Enter categories of expense (16MAX)", "Enter categories of income (10MAX)"],
        ["Hisa", "Placa"],
        ["Kite", "ViTech"],
    ],
    "2026": [
        [None, None, "Year"],
        [None, None, "Month", "Income", "Expenses", "Delta"],
        [None, None, "January", 2500.0, 71.55, 2428.45],
        [None, None, "February", 0.0, 4.14, -4.14],
    ],
}


def test_reads_and_imports_workbook(app):
    sheets = importer.read_ods(ods(WORKBOOK))
    with app.app_context():
        db = get_db()
        summary = importer.import_workbook(db, sheets, date(2026, 3, 1))
        assert summary == {"transactions": 5, "skipped rows": 1, "repeats": 1, "budgets": 2}

        txs = db.execute("SELECT * FROM transactions ORDER BY occurred_at").fetchall()
        assert [t["category"] for t in txs] == ["food", "gas", "placa", "bike", "clothing"]
        assert txs[0]["occurred_at"] == "2026-01-04 09:09:12"
        assert txs[1]["user_name"] is None
        assert (txs[2]["is_income"], txs[2]["amount_cents"]) == (1, 250000)
        assert txs[3]["amount_cents"] == -786
        assert txs[4]["comment"] == "5"

        rep = db.execute("SELECT * FROM repeats").fetchone()
        assert (rep["amount_cents"], rep["day_of_month"], rep["active_from"]) == (5721, 18, "2026-03-02")

        food = db.execute("SELECT budget_cents, budget_period FROM categories WHERE key = 'food'").fetchone()
        assert tuple(food) == (50000, "month")
        assert db.execute("SELECT label FROM categories WHERE key = 'kite'").fetchone()[0] == "Kite"

        assert importer.compare_with_year_sheets(db, sheets) == (2, [])


def test_second_import_needs_replace(app):
    sheets = importer.read_ods(ods(WORKBOOK))
    with app.app_context():
        db = get_db()
        importer.import_workbook(db, sheets, date(2026, 3, 1))
        with pytest.raises(ValueError, match="--replace"):
            importer.import_workbook(db, sheets, date(2026, 3, 1))
        summary = importer.import_workbook(db, sheets, date(2026, 3, 1), replace=True)
        assert summary["transactions"] == 5
        assert "repeats" not in summary  # kept as they were
        assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 5
