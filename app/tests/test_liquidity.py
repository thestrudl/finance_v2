from datetime import date

import pytest
from conftest import add_tx, csrf_from, login, message_update, post_update, texts

from finance import liquidity
from finance.db import get_db
from finance.parser import parse


def check(app, when, cents):
    with app.app_context():
        return liquidity.record_check(get_db(), cents, "web", "Ben", "", when)


def estimate(app, at):
    with app.app_context():
        return liquidity.estimate(get_db(), at)


def test_no_estimate_before_the_first_check(app):
    assert estimate(app, "2026-10-22 12:00:00") is None


def test_estimate_is_the_check_plus_what_was_logged_since(app):
    add_tx(app, "2026-10-01 09:00:00", 500_00, "food")              # before the check: already in it
    check(app, "2026-10-10 12:00:00", 4000_00)
    add_tx(app, "2026-10-12 09:00:00", 2600_00, "placa", is_income=True)
    add_tx(app, "2026-10-13 09:00:00", 150_00, "food")
    add_tx(app, "2026-10-14 09:00:00", -20_00, "bike")               # a refund
    est = estimate(app, "2026-10-22 12:00:00")
    assert (est["income"], est["expense"], est["entries"]) == (2600_00, 130_00, 3)
    assert est["amount"] == 4000_00 + 2600_00 - 130_00


def test_outside_our_accounts_does_not_move_the_estimate(app):
    check(app, "2026-10-10 12:00:00", 4000_00)
    tx = add_tx(app, "2026-10-12 09:00:00", 85000_00, "hisa", comment="gift paid straight to the seller")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE transactions SET off_balance = 1 WHERE id = ?", (tx,))
        db.commit()
    assert estimate(app, "2026-10-22 12:00:00")["amount"] == 4000_00


def test_a_new_check_shows_what_was_not_logged(app):
    check(app, "2026-10-01 12:00:00", 4000_00)
    add_tx(app, "2026-10-05 09:00:00", 100_00, "food")
    before, drift = check(app, "2026-10-20 12:00:00", 3850_00)
    assert before["amount"] == 3900_00 and drift == -50_00
    with app.app_context():
        newest, first = liquidity.checks(get_db())
    assert (newest["expected"], newest["drift"], newest["days"]) == (3900_00, -50_00, 19)
    assert "drift" not in first
    assert estimate(app, "2026-10-21 12:00:00")["amount"] == 3850_00  # the newest check wins


def test_daily_series(app):
    check(app, "2026-10-01 12:00:00", 1000_00)
    add_tx(app, "2026-10-02 09:00:00", 100_00, "food")
    add_tx(app, "2026-10-03 09:00:00", 50_00, "placa", is_income=True)
    check(app, "2026-10-04 08:00:00", 900_00)
    add_tx(app, "2026-10-04 10:00:00", 10_00, "food")
    with app.app_context():
        days, values, marks = liquidity.daily(get_db(), date(2026, 10, 5))
    assert days == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"]
    assert values == [1000_00, 900_00, 950_00, 890_00, 890_00]
    assert marks == [1000_00, None, None, 900_00, None]


@pytest.mark.parametrize("text, amount", [("balance", ""), ("Balance 4520.30", "4520.30"), ("/balance 4520,30", "4520,30")])
def test_parse_balance(text, amount):
    parsed = parse(text)
    assert (parsed.kind, parsed.amount) == ("balance", amount)


def test_telegram_balance(app, client, sent):
    post_update(client, message_update("balance"))
    assert texts(sent)[-1].startswith("No balance check yet.")
    post_update(client, message_update("balance 1000"))
    assert texts(sent)[-1] == "Balance check stored: €1,000.00 in the accounts and cash."
    with app.app_context():  # an entry in the very same second counts as already in the check
        db = get_db()
        db.execute("UPDATE balance_checks SET checked_at = datetime(checked_at, '-1 minute')")
        db.commit()
    post_update(client, message_update("food 25.50 spar"))
    post_update(client, message_update("balance"))
    assert texts(sent)[-1].startswith("Estimated bank & cash: €974.50")
    post_update(client, message_update("balance 960"))
    assert "€14.50 went out without being logged" in texts(sent)[-1]


def test_balance_check_from_the_portal(app, client, outbox):
    login(client, outbox)
    page = client.get("/holdings")
    assert "enter a balance check below" in page.get_data(as_text=True)
    client.post("/holdings/balance", data={"csrf": csrf_from(page), "amount": "4.520,30",
                                           "checked_at": "2026-10-20T09:15", "note": "OTP + Revolut"})
    add_tx(app, "2026-10-21 09:00:00", 20_30, "food")
    html = client.get("/holdings").get_data(as_text=True)
    assert "€4,500.00" in html and "OTP + Revolut" in html


def test_outside_flag_from_the_edit_form(app, client, outbox):
    login(client, outbox)
    tx = add_tx(app, "2026-10-12 09:00:00", 85000_00, "hisa")
    page = client.get(f"/transactions/{tx}/edit")
    client.post(f"/transactions/{tx}/edit", data={
        "csrf": csrf_from(page), "type": "expense", "category": "hisa", "amount": "85000",
        "occurred_at": "2026-10-12T09:00:00", "comment": "gift paid to the seller", "off_balance": "1",
    })
    with app.app_context():
        assert get_db().execute("SELECT off_balance FROM transactions WHERE id = ?", (tx,)).fetchone()[0] == 1
    assert "outside" in client.get("/transactions").get_data(as_text=True)
