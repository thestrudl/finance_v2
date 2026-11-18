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
