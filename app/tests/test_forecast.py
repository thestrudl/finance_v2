from datetime import date

import pytest
from conftest import add_tx, login

from finance import forecast, liquidity
from finance.db import get_db
from finance.util import now, shift_month, ts

TODAY = date(2026, 11, 1)


def add_repeat(app, cents, category, day, is_income=False, active_from="2026-01-01"):
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO repeats (user_name, amount_cents, category, comment, is_income, day_of_month,
                   active_from, created_at) VALUES ('Ben', ?, ?, '', ?, ?, ?, ?)""",
            (cents, category, int(is_income), day, active_from, "2026-01-01 00:00:00"),
        )
        db.commit()


def build(app, until, start=1000_00, planned=(), fund=0):
    with app.app_context():
        return forecast.build(get_db(), TODAY, until, start, planned, fund)


@pytest.mark.parametrize("ym, source", [
    ("2026-11", "2025-11"), ("2026-12", "2025-12"), ("2027-10", "2026-10"), ("2027-11", "2025-11"),
])
def test_source_month_is_the_latest_full_year(ym, source):
    assert forecast.source_month(ym, "2026-11") == source


@pytest.fixture
def year_of_history(app):
    for i in range(12):
        ym = shift_month("2025-11", i)
        add_tx(app, f"{ym}-05 10:00:00", 300_00, "food")
        add_tx(app, f"{ym}-18 10:00:00", 50_00, "insurance", source="import")  # logged by the old repeat
    add_tx(app, "2025-11-15 10:00:00", 2000_00, "placa", is_income=True)
    add_tx(app, "2025-12-15 10:00:00", 2000_00, "placa", is_income=True)
    add_tx(app, "2025-12-03 10:00:00", 100_00, "sunny")              # a business cost: not projected
    add_tx(app, "2025-12-10 10:00:00", 9000_00, "hisa")              # a one-off project cost
    add_repeat(app, 50_00, "Insurance", 18)


def test_forecast_month_by_month(app, year_of_history):
    planned = [{"day": date(2026, 12, 20), "cents": -500_00, "label": "car"}]
    result = build(app, date(2026, 12, 31), planned=planned, fund=800_00)
    nov, dec = result["months"]
    assert (nov["income"], nov["repeat_out"], nov["living"], nov["net"]) == (2000_00, 50_00, 300_00, 1650_00)
    assert (dec["income"], dec["planned"], dec["net"]) == (2000_00, -500_00, 1150_00)  # last December's bikes don't come back
    assert "business" not in dec
    assert (nov["end"], dec["end"]) == (2650_00, 3800_00) and result["end"] == 3800_00
    assert (nov["first"], nov["last"], nov["source"], nov["estimated"]) == (date(2026, 11, 2), date(2026, 11, 30),
                                                                            "2025-11", False)
    # Living costs land on the 5th, income only on the 15th: the low point is in between.
    assert result["low"] == {"day": date(2026, 11, 5), "amount": 700_00}
    assert result["below_fund"] == {"day": date(2026, 11, 5), "amount": 700_00}
    assert len(result["days"]) == len(result["balances"]) == 60


def test_repeat_that_is_not_active_yet(app, year_of_history):
    add_repeat(app, 1000_00, "app", 1, is_income=True, active_from="2026-12-01")
    nov, dec = build(app, date(2026, 12, 31))["months"]
    assert (nov["repeat_in"], dec["repeat_in"]) == (0, 1000_00)


def test_month_without_history_uses_the_average_month(app):
    add_tx(app, "2026-10-10 10:00:00", 3000_00, "placa", is_income=True)
    (nov,) = build(app, date(2026, 11, 30))["months"]
    assert nov["estimated"] and nov["income"] == 2900_00  # €100 a day from the 2nd


def test_outside_our_accounts_is_left_out(app):
    tx = add_tx(app, "2025-11-20 10:00:00", 5000_00, "placa", is_income=True)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE transactions SET off_balance = 1 WHERE id = ?", (tx,))
        db.commit()
    assert build(app, date(2026, 11, 30))["months"][0]["income"] == 0


@pytest.fixture
def logged_in(client, outbox):
    login(client, outbox)
    return client


def test_forecast_page(app, logged_in):
    with app.app_context():
        moment = now()
        liquidity.record_check(get_db(), 4000_00, "web", "Ben", "", ts(moment))
        liquidity.set_security_fund(get_db(), 1000_00)
    page = logged_in.get("/forecast?preset=3m&p_day=2099-01-01&p_amount=100&p_way=out&p_label=far+away"
                         "&p_day=&p_amount=abc&p_way=out&p_label=broken").get_data(as_text=True)
    assert "Free to use" in page and "€3,000.00" in page
    assert "outside the forecast" in page and "needs a day and an amount" in page
    assert 'id="forecast-chart"' in page


def test_forecast_page_without_a_balance_check(logged_in):
    page = logged_in.get("/forecast?until=nonsense").get_data(as_text=True)
    assert "no balance check yet" in page.lower()


def test_forecast_needs_login(client):
    assert client.get("/forecast").headers["Location"].startswith("/login")


def test_business_income_counts_but_business_costs_dont(app):
    """Rentals come back with the season; what a business spends is up to us."""
    for i in range(12):
        add_tx(app, f"{shift_month('2025-11', i)}-05 10:00:00", 300_00, "food")
    add_tx(app, "2025-12-12 10:00:00", 1500_00, "sunny", is_income=True)   # bike rentals last December
    add_tx(app, "2025-12-14 10:00:00", 4000_00, "sunny")                   # new bikes last December
    result = build(app, date(2026, 12, 31))
    dec = result["months"][1]
    assert dec["income"] == 1500_00
    assert dec["net"] == 1500_00 - dec["living"]
