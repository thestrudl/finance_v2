from datetime import datetime

import pytest
from conftest import add_tx, make_app

from finance import api, liquidity
from finance.db import get_db

TOKEN = "api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "now", lambda: datetime(2026, 11, 18, 12, 0))
    monkeypatch.setattr(liquidity, "now", lambda: datetime(2026, 11, 18, 12, 0))
    return make_app(tmp_path, FINANCE_API_TOKEN=TOKEN)


def test_no_token_configured_means_no_api(tmp_path):
    client = make_app(tmp_path).test_client()
    assert client.get("/api/v1/liquidity", headers=AUTH).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN + "x"}])
def test_wrong_or_missing_token_is_refused(app, headers):
    assert app.test_client().get("/api/v1/liquidity", headers=headers).status_code == 401


def test_liquidity_without_a_balance_check(app):
    body = app.test_client().get("/api/v1/liquidity?until=2026-12-31", headers=AUTH).get_json()
    assert body["today"] == "2026-11-18"
    assert body["balance"] is None
    assert body["forecast"]["days"][0] == "2026-11-19"
    assert body["forecast"]["days"][-1] == "2026-12-31"
    assert len(body["forecast"]["balances"]) == len(body["forecast"]["days"])
    assert "category_spend" not in body


def test_liquidity_with_balance_fund_and_category(app):
    with app.app_context():
        db = get_db()
        liquidity.record_check(db, 20_000_00, "web", "Ben", at="2026-11-01 09:00:00")
        liquidity.set_security_fund(db, 5_000_00)
    add_tx(app, "2026-11-10 10:00:00", 1_000_00, "hisa")
    add_tx(app, "2026-10-03 10:00:00", 400_00, "hisa")
    add_tx(app, "2026-10-04 10:00:00", -50_00, "hisa")  # refund

    body = app.test_client().get("/api/v1/liquidity?category=Hisa", headers=AUTH).get_json()
    assert body["balance"]["amount_cents"] == 19_000_00
    assert body["balance"]["checked_at"] == "2026-11-01 09:00:00"
    assert body["security_fund_cents"] == 5_000_00
    spend = body["category_spend"]
    assert spend["found"] and spend["category"] == "hisa"
    assert spend["months"] == [{"month": "2026-10", "cents": 350_00}, {"month": "2026-11", "cents": 1_000_00}]
    assert spend["total_cents"] == 1_350_00


def test_unknown_category_is_reported_not_created(app):
    body = app.test_client().get("/api/v1/liquidity?category=nothing", headers=AUTH).get_json()
    assert body["category_spend"] == {"category": "nothing", "found": False, "months": [], "total_cents": 0}
    with app.app_context():
        assert get_db().execute("SELECT 1 FROM categories WHERE key = 'nothing'").fetchone() is None


def test_until_is_clamped(app):
    body = app.test_client().get("/api/v1/liquidity?until=2040-01-01", headers=AUTH).get_json()
    assert len(body["forecast"]["days"]) <= 3 * 366


def put(client, key, **body):
    payload = {"day": "2026-11-10", "amount_cents": 500_00, "category": "hisa", "comment": "Renovation · Doors",
               "user_name": "Ben Novak"}
    payload.update(body)
    return client.put(f"/api/v1/transactions/{key}", json=payload, headers=AUTH)


def transactions(app):
    with app.app_context():
        return [dict(r) for r in get_db().execute("SELECT * FROM transactions ORDER BY id")]


@pytest.fixture
def house(app):
    add_tx(app, "2026-10-01 10:00:00", 1_00, "hisa")  # makes the category exist
    with app.app_context():
        db = get_db()
        db.execute("DELETE FROM transactions")
        db.commit()
    return app


def test_put_logs_an_expense_once(house):
    client = house.test_client()
    first = put(client, "renovation-payment-abc")
    assert first.status_code == 201 and first.get_json()["created"]
    again = put(client, "renovation-payment-abc")  # a retry
    assert again.status_code == 200 and not again.get_json()["created"]
    rows = transactions(house)
    assert len(rows) == 1
    row = rows[0]
    assert (row["occurred_at"], row["amount_cents"], row["category"], row["is_income"], row["source"]) == (
        "2026-11-10 12:00:00", 500_00, "hisa", 0, "web")
    assert (row["comment"], row["user_name"], row["off_balance"]) == ("Renovation · Doors", "Ben Novak", 0)


def test_put_changes_and_delete_removes(house):
    client = house.test_client()
    put(client, "k1")
    put(client, "k1", amount_cents=650_00, day="2026-11-18", off_balance=True, comment="changed")
    row = transactions(house)[0]
    assert (row["amount_cents"], row["occurred_at"], row["off_balance"], row["comment"]) == (
        650_00, "2026-11-18 12:00:00", 1, "changed")  # today: the time it was entered (noon in this test)
    assert client.delete("/api/v1/transactions/k1", headers=AUTH).get_json() == {"deleted": True}
    assert transactions(house) == []
    assert client.delete("/api/v1/transactions/k1", headers=AUTH).get_json() == {"deleted": False}


def test_put_after_the_transaction_was_deleted_in_finance_logs_it_again(house):
    client = house.test_client()
    tx_id = put(client, "k2").get_json()["transaction_id"]
    with house.app_context():
        db = get_db()
        db.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        db.commit()
    assert put(client, "k2").get_json()["created"]
    assert len(transactions(house)) == 1


@pytest.mark.parametrize("body, message", [
    ({"day": "soon"}, "day"),
    ({"amount_cents": 0}, "amount_cents"),
    ({"amount_cents": "500"}, "amount_cents"),
    ({"amount_cents": True}, "amount_cents"),
    ({"category": "nothing"}, "no category"),
])
def test_put_validation(house, body, message):
    response = put(house.test_client(), "k3", **body)
    assert response.status_code == 400 and message in response.get_json()["error"]
    assert transactions(house) == []


def test_put_rejects_odd_keys_and_needs_the_token(house):
    client = house.test_client()
    assert put(client, "bad key!").status_code in (400, 404)
    response = client.put("/api/v1/transactions/k4", json={}, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert client.delete("/api/v1/transactions/k4").status_code == 401


def test_list_transactions_in_a_category(house):
    add_tx(house, "2026-09-01 10:00:00", 2_000_00, "hisa", comment="Kitchen deposit")
    add_tx(house, "2026-10-05 10:00:00", 800_00, "Hisa", comment="Parquet samples")
    add_tx(house, "2026-10-06 10:00:00", 50_00, "food")
    add_tx(house, "2026-10-07 10:00:00", 100_00, "hisa", is_income=True)  # not an expense
    client = house.test_client()
    put(client, "renovation-payment-1", comment="from the planner")
    body = client.get("/api/v1/transactions?category=Hisa", headers=AUTH).get_json()
    assert body["category"] == "hisa"
    rows = body["transactions"]
    assert [(r["occurred_at"][:10], r["amount_cents"], r["comment"], r["api_key"]) for r in rows] == [
        ("2026-11-10", 500_00, "from the planner", "renovation-payment-1"),
        ("2026-10-05", 800_00, "Parquet samples", None),
        ("2026-09-01", 2_000_00, "Kitchen deposit", None),
    ]
    assert rows[1]["off_balance"] is False and "id" in rows[1]
    assert client.get("/api/v1/transactions?category=nope", headers=AUTH).status_code == 400
    assert client.get("/api/v1/transactions?category=hisa").status_code == 401
