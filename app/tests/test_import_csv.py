from datetime import datetime, timedelta

import pytest

from finance import importer
from finance.db import get_db
from finance.util import now

# What Google Sheets saves for the webHookData tab in the en-US locale.
GOOGLE_US = (
    "date time,User,Price,Category,Comment,Income true false\r\n"
    "5/4/2024 9:09:12,Ben N,6.04,Food,hoferic,FALSE\r\n"
    "5/4/2024 13:11:01,undefined undefined,65.51,Gas,,FALSE\r\n"
    "11/10/2025 3:14:41,,,,,\r\n"
    "5/20/2024 10:00:00,Ana Novak,2500,placa,\"salary, may\",TRUE\r\n"
    "6/3/2024 11:02:21,Ben N,-7.86,Bike,partial refund,FALSE\r\n"
)

# A day-first export with decimal commas and ; between fields.
EUROPEAN = (
    "﻿Datum;Uporabnik;Cena;Kategorija;Komentar;Prihodek\n"
    "4. 05. 2024 09:09:12;Ben N;6,04;Food;hoferic;NEPRAVILNO\n"
    "20.05.2024 10:00;Ana Novak;1.234,50;placa;placa;PRAVILNO\n"
)


def run_cli(app, args, data):
    return app.test_cli_runner().invoke(args=args, input=data, catch_exceptions=False)


def rows(app):
    with app.app_context():
        return [dict(r) for r in get_db().execute("SELECT * FROM transactions ORDER BY occurred_at")]


@pytest.mark.parametrize("text, value", [
    ("10.000", 10000.0), ("4,520", 4520.0), ("0.500", 0.5), ("4.52", 4.52),
    ("6.04", 6.04), ("6,04", 6.04), ("-7.86", -7.86), ("2500", 2500.0), ("1,234.56", 1234.56),
    ("1.234,56", 1234.56), ("€ 12,50", 12.5), ("1 234,5", 1234.5), ("", None), ("abc", None),
])
def test_parse_amount(text, value):
    assert importer.parse_amount(text) == value


@pytest.mark.parametrize("text, value", [
    ("FALSE", False), ("TRUE", True), ("true", True), ("0", False), ("1", True), ("", False),
    ("PRAVILNO", True), ("NEPRAVILNO", False), ("maybe", None),
])
def test_parse_flag(text, value):
    assert importer.parse_flag(text) == value


def test_date_order_is_worked_out_from_the_column():
    dates, order = importer.parse_dates(["5/4/2024 9:09:12", "5/20/2024 10:00:00"])
    assert order == "mdy" and dates[0] == datetime(2024, 5, 4, 9, 9, 12)
    dates, order = importer.parse_dates(["4.5.2024 9:09", "20.5.2024 10:00"])
    assert order == "dmy" and dates[0] == datetime(2024, 5, 4, 9, 9)
    dates, order = importer.parse_dates(["2024-05-04 09:09:12", "2024-05-04T13:11:01.334"])
    assert order == "ymd" and dates[1] == datetime(2024, 5, 4, 13, 11, 1)
    dates, _ = importer.parse_dates(["5/4/2024 9:09:12 PM"])
    assert dates[0].hour == 21


def test_mixed_date_orders_are_refused():
    with pytest.raises(ValueError, match="--date-order"):
        importer.parse_dates(["13/1/2024", "1/13/2024"])


def test_google_us_csv(app):
    result = run_cli(app, ["import-csv", "-"], GOOGLE_US)
    assert "transactions: 4" in result.output
    assert "skipped rows: 1" in result.output  # the date-only leftover of a cleared repeat
    assert "Dates read as m/d/y" in result.output
    txs = rows(app)
    assert [t["occurred_at"] for t in txs] == [
        "2024-05-04 09:09:12", "2024-05-04 13:11:01", "2024-05-20 10:00:00", "2024-06-03 11:02:21"]
    assert txs[1]["user_name"] is None and txs[1]["comment"] == ""
    assert (txs[2]["is_income"], txs[2]["amount_cents"], txs[2]["comment"]) == (1, 250000, "salary, may")
    assert txs[3]["amount_cents"] == -786
    assert "2024: 4 entries" in result.output


def test_european_csv(app):
    result = run_cli(app, ["import-csv", "-"], EUROPEAN)
    assert "transactions: 2" in result.output and "d/m/y" in result.output
    txs = rows(app)
    assert txs[0]["occurred_at"] == "2024-05-04 09:09:12"
    assert (txs[1]["amount_cents"], txs[1]["is_income"]) == (123450, 1)


def test_unreadable_income_cell_skips_the_row(app):
    result = run_cli(app, ["import-csv", "-"], "1/2/2024 10:00:00,Ben,5,food,x,MAYBE\n")
    assert "skipped rows: 1" in result.output
    assert rows(app) == []


def test_import_again_needs_replace(app):
    run_cli(app, ["import-csv", "-"], GOOGLE_US)
    result = app.test_cli_runner().invoke(args=["import-csv", "-"], input=GOOGLE_US)
    assert result.exit_code != 0 and "--replace" in result.output
    run_cli(app, ["import-csv", "--replace", "-"], GOOGLE_US)
    assert len(rows(app)) == 4


def test_repeats_csv(app):
    data = (
        "Date Time,User,Price,Category,Comment,Income,dayOfLogging\n"
        "5/28/2024 17:59:53,Ana Novak,57.21,insurance,life insurance,FALSE,18\n"
        ",,,,,,10\n"
    )
    result = run_cli(app, ["import-csv", "--repeats", "-"], data)
    assert "repeats: 1" in result.output
    with app.app_context():
        rep = get_db().execute("SELECT * FROM repeats").fetchone()
        tomorrow = (now().date() + timedelta(days=1)).isoformat()
    assert (rep["amount_cents"], rep["day_of_month"], rep["is_income"]) == (5721, 18, 0)
    assert rep["active_from"] == tomorrow  # the sheet already logged everything due so far
    assert "kept" in run_cli(app, ["import-csv", "--repeats", "-"], data).output
