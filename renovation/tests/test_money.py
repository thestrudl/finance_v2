from datetime import date, timedelta

import pytest
import requests
from conftest import TODAY, add_goal, add_task, build_plan

from renovation import money


def d(s):
    return date.fromisoformat(s)


def finance_body(days=60, start=20_000_00, fund=5_000_00, daily=0, category=True):
    first = TODAY + timedelta(days=1)
    body = {
        "today": TODAY.isoformat(),
        "balance": {"amount_cents": start, "checked_at": "2026-11-15 10:00:00", "entries_since": 3},
        "security_fund_cents": fund,
        "forecast": {
            "until": (first + timedelta(days=days - 1)).isoformat(),
            "days": [(first + timedelta(days=i)).isoformat() for i in range(days)],
            "balances": [start + daily * (i + 1) for i in range(days)],
            "history_months": 12,
        },
    }
    if category:
        body["category_spend"] = {"category": "hisa", "label": "Hisa", "found": True,
                                  "months": [{"month": "2026-11", "cents": 3_000_00}], "total_cents": 3_000_00}
    return body


def test_payments(app):
    goal = add_goal(app, "Doors", "2027-02-01")
    add_task(app, goal, "Materials", days=3, cost=2_000_00, pay_at="start", not_before="2026-12-01")
    add_task(app, goal, "Fitter", days=3, cost=1_500_00, paid=500_00, pay_at="end", not_before="2026-12-01")
    add_task(app, goal, "Done already", cost=900_00, status="done", done_on="2026-11-10")
    add_task(app, goal, "Free work", days=2)
    add_task(app, goal, "Overdue deposit", cost=300_00, status="doing", started_on="2026-11-01")
    pays = money.payments(build_plan(app))
    assert [(p["day"], p["cents"], p["label"], p["overdue"]) for p in pays] == [
        (d("2026-11-19"), 300_00, "Overdue deposit", True),   # due 1 Nov, unpaid: counts tomorrow
        (d("2026-12-01"), 2_000_00, "Materials", False),
        (d("2026-12-03"), 1_000_00, "Fitter", False),          # what's left, on the last day
    ]


def test_rough_budget_until_tasks_are_priced(app):
    garden = add_goal(app, "Garden", "2027-04-01", budget=5_000_00)
    add_task(app, garden, "Plan it")  # no cost: the budget still stands in
    priced = add_goal(app, "Roof", "2027-03-01", budget=9_000_00)
    add_task(app, priced, "Tiles", cost=7_000_00)
    pays = money.payments(build_plan(app))
    assert [(p["goal"], p["cents"], p["rough"]) for p in pays] == [("Roof", 7_000_00, False), ("Garden", 5_000_00, True)]
    assert pays[1]["day"] == d("2027-04-01")


def test_horizon(app):
    goal = add_goal(app, "Roof", "2027-03-01")
    add_task(app, goal, "Tiles", cost=100_00, not_before="2027-02-01", days=10)
    current = build_plan(app)
    assert money.horizon(current, money.payments(current)) == d("2027-03-01") + timedelta(days=31)
    add_goal(app, "Soon", "2026-11-25")
    assert money.horizon(build_plan(app), []) == d("2027-04-01")  # Roof's deadline, a month on


def test_overlay():
    pays = [{"day": TODAY + timedelta(days=10), "cents": 8_000_00}, {"day": TODAY + timedelta(days=20), "cents": 9_000_00}]
    result = money.overlay(finance_body(days=30, daily=10_00), pays)
    assert result["start"] == 20_000_00
    assert result["without"][-1] == 20_300_00
    assert result["with"][9] == 20_100_00 - 8_000_00
    assert result["with"][-1] == 20_300_00 - 17_000_00
    assert result["low"] == {"day": TODAY + timedelta(days=20), "amount": 20_200_00 - 17_000_00}
    assert result["spare"] == 3_200_00 - 5_000_00
    assert result["below_fund"]["day"] == TODAY + timedelta(days=20)
    assert result["below_zero"] is None
    assert [m["month"] for m in result["months"]] == ["2026-11", "2026-12"]
    assert sum(m["renovation"] for m in result["months"]) == 17_000_00


def test_overlay_low_point_is_today_when_money_only_grows():
    result = money.overlay(finance_body(days=10, daily=50_00), [])
    assert result["low"] == {"day": TODAY, "amount": 20_000_00}
    assert result["spare"] == 15_000_00
    assert result["below_fund"] is None


def test_overlay_without_balance_check():
    body = finance_body(days=5)
    body["balance"] = None
    body["forecast"]["balances"] = [0] * 5
    result = money.overlay(body, [])
    assert result["start"] == 0


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.body


def test_fetch_liquidity_sends_token_and_category(app, monkeypatch):
    calls = []

    def fake_get(url, params, timeout, headers):
        calls.append((url, params, headers))
        return FakeResponse({"ok": True})

    monkeypatch.setattr(money.requests, "get", fake_get)
    with app.app_context():
        assert money.fetch_liquidity(d("2027-03-01")) == ({"ok": True}, None)
    assert calls == [("http://finance:8000/api/v1/liquidity", {"until": "2027-03-01", "category": "hisa"},
                      {"Authorization": "Bearer api-token"})]


@pytest.mark.parametrize("failure", [requests.ConnectionError("down"), FakeResponse({}, 401)])
def test_fetch_liquidity_failures(app, monkeypatch, failure):
    def fake_get(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(money.requests, "get", fake_get)
    with app.app_context():
        body, problem = money.fetch_liquidity(d("2027-03-01"))
    assert body is None and "didn't answer" in problem


def test_fetch_liquidity_not_configured(tmp_path):
    from conftest import make_app

    app = make_app(tmp_path, FINANCE_API_TOKEN="")
    with app.app_context():
        body, problem = money.fetch_liquidity(d("2027-03-01"))
    assert body is None and "FINANCE_API_TOKEN" in problem


def test_money_page(logged_in, app, monkeypatch):
    goal = add_goal(app, "Doors", "2027-01-15")
    add_task(app, goal, "Fitter", cost=16_000_00, not_before="2026-12-10")
    monkeypatch.setattr(money, "fetch_liquidity", lambda until: (finance_body(days=(until - TODAY).days), None))
    page = logged_in.get("/money").get_data(as_text=True)
    assert "Free to invest now" in page
    assert "−€1,000" in page  # 20,000 - 16,000 - 5,000 fund: short by 1,000
    assert "into the security fund from 10 Dec" in page
    assert "Logged in finance under Hisa" in page
    assert 'id="money-chart"' in page


def test_money_page_without_finance(logged_in, app, monkeypatch):
    goal = add_goal(app, "Doors", "2027-01-15")
    add_task(app, goal, "Fitter", cost=1_000_00)
    monkeypatch.setattr(money, "fetch_liquidity", lambda until: (None, "The finance app didn't answer."))
    page = logged_in.get("/money").get_data(as_text=True)
    assert "The finance app didn&#39;t answer." in page
    assert "€1,000" in page
    assert "Free to invest now" not in page
