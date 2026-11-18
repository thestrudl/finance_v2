"""Payments, commitments, and logging payments in finance."""
import sqlite3
from datetime import date

import pytest
import requests
from conftest import TODAY, add_goal, add_task, build_plan, csrf_from, make_app

from renovation import ledger, money
from renovation.db import get_db


def d(s):
    return date.fromisoformat(s)


class FakeFinance:
    """Stands in for finance's /api/v1/transactions: remembers what's logged by key."""

    def __init__(self):
        self.entries, self.calls, self.down = {}, [], False

    def request(self, method, url, json=None, timeout=None, headers=None):
        self.calls.append((method, url, json, headers))
        if self.down:
            raise requests.ConnectionError("down")
        key = url.rsplit("/", 1)[1]
        response = requests.Response()
        response.status_code = 200
        if method == "PUT":
            self.entries[key] = json
            response._content = b'{"transaction_id": 1, "created": true}'
        else:
            response._content = b'{"deleted": %s}' % (b"true" if self.entries.pop(key, None) else b"false")
        return response


@pytest.fixture
def finance(monkeypatch):
    fake = FakeFinance()
    monkeypatch.setattr(ledger.requests, "request", fake.request)
    return fake


def payments(app):
    with app.app_context():
        return [dict(r) for r in get_db().execute("SELECT * FROM payments ORDER BY id")]


def add_commitment(app, goal_id, what, cents, task_id=None, due_on=None):
    with app.app_context():
        db = get_db()
        cur = db.execute("""INSERT INTO commitments (goal_id, task_id, what, amount_cents, due_on, created_at)
                            VALUES (?, ?, ?, ?, ?, 'x')""", (goal_id, task_id, what, cents, due_on))
        db.commit()
        return cur.lastrowid


# ── cash flow ──

def test_commitments_are_firm_and_cover_their_task(app):
    goal = add_goal(app, "Doors", "2027-03-01")
    parquet = add_task(app, goal, "Parquet", days=5, cost=5_000_00, not_before="2026-12-10", pay_at="end")
    add_commitment(app, goal, "Parquet offer", 4_500_00, task_id=parquet, due_on="2027-01-10")
    with app.app_context():
        db = get_db()
        cid = db.execute("SELECT id FROM commitments").fetchone()[0]
        db.execute("""INSERT INTO payments (goal_id, task_id, commitment_id, paid_on, amount_cents, created_at)
                      VALUES (?, ?, ?, '2026-11-15', 100000, 'x')""", (goal, parquet, cid))
        db.commit()
    current = build_plan(app)
    g = current["goals"][0]
    assert (g["paid"], g["committed"], g["committed_open"]) == (1_000_00, 4_500_00, 3_500_00)
    pays = money.payments(current)
    # 3,500 still open on the offer, on its due day; 500 of the estimate isn't covered by anything yet.
    assert [(p["day"], p["cents"], p["kind"]) for p in pays] == [
        (d("2026-12-14"), 500_00, "estimate"),
        (d("2027-01-10"), 3_500_00, "committed"),
    ]


def test_done_task_still_owes_its_open_commitment(app):
    goal = add_goal(app, "Doors", "2027-03-01")
    fitter = add_task(app, goal, "Fitter", cost=2_000_00, status="done", done_on="2026-11-10")
    add_commitment(app, goal, "Fitter invoice", 1_800_00, task_id=fitter)
    pays = money.payments(build_plan(app))
    # No due day: its task's pay day (10 Nov, past) -> tomorrow.
    assert [(p["day"], p["cents"], p["kind"], p["overdue"]) for p in pays] == [
        (d("2026-11-19"), 1_800_00, "committed", True)]


def test_whole_item_payments_cover_task_estimates_earliest_first(app):
    goal = add_goal(app, "Bathroom", "2027-03-01")
    add_task(app, goal, "Tiles", cost=1_000_00, not_before="2026-12-01")
    add_task(app, goal, "Plumbing", cost=800_00, not_before="2026-12-20")
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO payments (goal_id, paid_on, amount_cents, what, created_at)
                      VALUES (?, '2026-11-10', 120000, 'Materials for the whole bathroom', 'x')""", (goal,))
        db.commit()
    pays = money.payments(build_plan(app))
    assert [(p["label"], p["cents"]) for p in pays] == [("Plumbing", 600_00)]


def test_rough_budget_less_paid_and_committed(app):
    goal = add_goal(app, "Garden", "2027-04-01", budget=6_000_00)
    add_commitment(app, goal, "Fence", 2_000_00, due_on="2027-02-01")
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO payments (goal_id, paid_on, amount_cents, created_at) VALUES (?, '2026-11-01', 50000, 'x')",
                   (goal,))
        db.commit()
    pays = money.payments(build_plan(app))
    assert [(p["day"], p["cents"], p["kind"]) for p in pays] == [
        (d("2027-02-01"), 2_000_00, "committed"), (d("2027-04-01"), 3_500_00, "budget")]


# ── migration ──

def test_paid_so_far_becomes_a_payment(tmp_path):
    path = tmp_path / "renovation.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE goals (id INTEGER PRIMARY KEY, title TEXT NOT NULL, deadline TEXT NOT NULL, budget_cents INTEGER,
            notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            title TEXT NOT NULL, days INTEGER NOT NULL DEFAULT 1, not_before TEXT, status TEXT NOT NULL DEFAULT 'todo',
            started_on TEXT, done_on TEXT, cost_cents INTEGER NOT NULL DEFAULT 0, paid_cents INTEGER NOT NULL DEFAULT 0,
            pay_at TEXT NOT NULL DEFAULT 'start', who TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO goals VALUES (1, 'Doors', '2027-05-01', NULL, '', 'x');
        INSERT INTO tasks (id, goal_id, title, status, done_on, cost_cents, paid_cents, created_at, updated_at)
            VALUES (1, 1, 'Fitter', 'done', '2026-11-10', 800, 800, 'x', '2026-11-10 10:00:00'),
                   (2, 1, 'Walls', 'todo', NULL, 500, 0, 'x', 'x');
        PRAGMA user_version = 1;
    """)
    conn.close()
    app = make_app(tmp_path)
    with app.app_context():
        db = get_db()
        assert "paid_cents" not in {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
        rows = [dict(r) for r in db.execute("SELECT * FROM payments")]
    assert len(rows) == 1
    assert (rows[0]["task_id"], rows[0]["paid_on"], rows[0]["amount_cents"], rows[0]["finance"]) == (
        1, "2026-11-10", 800, "skip")


# ── pages ──

def post(client, path, **data):
    data.setdefault("csrf", csrf_from(client.get("/")))
    return client.post(path, data=data)


def test_payment_is_logged_in_finance(logged_in, app, finance):
    goal = add_goal(app, "Doors, 2nd floor", "2027-05-01")
    task = add_task(app, goal, "Parquet", cost=4_500_00)
    response = post(logged_in, f"/goals/{goal}/payments", amount="1.500,00", paid_on="2026-11-17", what="Deposit",
                    task_id=str(task), log="1")
    assert response.status_code == 302
    row = payments(app)[0]
    assert (row["amount_cents"], row["task_id"], row["finance"], row["user_name"]) == (
        1_500_00, task, "logged", "Ben Novak")
    method, url, body, headers = finance.calls[-1]
    assert method == "PUT" and url == f"http://finance:8000/api/v1/transactions/{row['finance_key']}"
    assert body == {"day": "2026-11-17", "amount_cents": 1_500_00, "category": "hisa",
                    "comment": "Renovation · Doors, 2nd floor · Deposit", "user_name": "Ben Novak",
                    "off_balance": False}
    assert headers == {"Authorization": "Bearer api-token"}
    page = logged_in.get(f"/goals/{goal}").get_data(as_text=True)
    assert "in finance" in page and "€1,500" in page


def test_unticked_payment_stays_out_of_finance(logged_in, app, finance):
    goal = add_goal(app, "Doors", "2027-05-01")
    post(logged_in, f"/goals/{goal}/payments", amount="200", paid_on="2026-11-17", what="Logged on Telegram")
    assert payments(app)[0]["finance"] == "skip" and payments(app)[0]["finance_key"] is None
    assert finance.calls == []


def test_finance_down_then_retry(logged_in, app, finance):
    goal = add_goal(app, "Doors", "2027-05-01")
    finance.down = True
    response = post(logged_in, f"/goals/{goal}/payments", amount="300", paid_on="2026-11-17", log="1")
    assert response.status_code == 302
    row = payments(app)[0]
    assert row["finance"] == "failed" and row["finance_error"] == "finance didn't answer"
    assert "didn" in logged_in.get("/money").get_data(as_text=True)  # the warning on the money page
    finance.down = False
    post(logged_in, f"/payments/{row['id']}/retry")
    assert payments(app)[0]["finance"] == "logged"
    assert finance.entries[row["finance_key"]]["amount_cents"] == 300_00


def test_edit_untick_and_delete_follow_in_finance(logged_in, app, finance):
    goal = add_goal(app, "Doors", "2027-05-01")
    post(logged_in, f"/goals/{goal}/payments", amount="300", paid_on="2026-11-17", log="1")
    row = payments(app)[0]
    key = row["finance_key"]
    post(logged_in, f"/payments/{row['id']}", amount="350", paid_on="2026-11-16", what="Fixed", log="1")
    assert finance.entries[key]["amount_cents"] == 350_00 and finance.entries[key]["day"] == "2026-11-16"
    post(logged_in, f"/payments/{row['id']}", amount="350", paid_on="2026-11-16", what="Fixed")  # unticked
    assert key not in finance.entries and payments(app)[0]["finance"] == "skip"
    post(logged_in, f"/payments/{row['id']}", amount="350", paid_on="2026-11-16", what="Fixed", log="1")
    new_key = payments(app)[0]["finance_key"]
    assert new_key in finance.entries
    post(logged_in, f"/payments/{row['id']}/delete")
    assert payments(app) == [] and finance.entries == {}


def test_delete_refused_while_finance_is_down(logged_in, app, finance):
    goal = add_goal(app, "Doors", "2027-05-01")
    post(logged_in, f"/goals/{goal}/payments", amount="300", paid_on="2026-11-17", log="1")
    finance.down = True
    post(logged_in, f"/payments/{payments(app)[0]['id']}/delete")
    assert len(payments(app)) == 1
    post(logged_in, f"/goals/{goal}/delete")
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 1
    finance.down = False
    post(logged_in, f"/goals/{goal}/delete")
    assert finance.entries == {}
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0


def test_payment_validation(logged_in, app, finance):
    goal = add_goal(app, "Doors", "2027-05-01")
    response = post(logged_in, f"/goals/{goal}/payments", amount="lots", paid_on="", log="1")
    assert response.status_code == 400
    text = response.get_data(as_text=True)
    assert "isn&#39;t an amount" in text and "When was it paid" in text
    assert payments(app) == [] and finance.calls == []


def test_without_finance_settings_nothing_is_logged(tmp_path, outbox, monkeypatch):
    from conftest import login

    app = make_app(tmp_path, FINANCE_CATEGORY="")
    client = app.test_client()
    login(client, outbox)
    goal = add_goal(app, "Doors", "2027-05-01")
    page = client.get(f"/goals/{goal}").get_data(as_text=True)
    assert "aren't logged in finance" in page
    post(client, f"/goals/{goal}/payments", amount="100", paid_on="2026-11-17", log="1")
    assert payments(app)[0]["finance"] == "skip"


def test_commitments_and_paying_towards_one(logged_in, app, finance):
    goal = add_goal(app, "Doors", "2027-05-01")
    task = add_task(app, goal, "Parquet", cost=5_000_00)
    response = post(logged_in, f"/goals/{goal}/commitments", what="Parquet offer", who="Parket d.o.o.",
                    amount="4500", due_on="2027-01-10", task_id=str(task), notes="Offer 2026-118")
    assert response.status_code == 302
    with app.app_context():
        cid = get_db().execute("SELECT id FROM commitments").fetchone()[0]
    # "Pay" opens the payment form filled in with what's open.
    page = logged_in.get(f"/goals/{goal}?commitment={cid}").get_data(as_text=True)
    assert 'value="4500.00"' in page and "Paying towards" in page
    post(logged_in, f"/goals/{goal}/payments", amount="4500", paid_on="2026-11-18", what="Parquet offer",
         commitment_id=str(cid), log="1")
    row = payments(app)[0]
    assert row["commitment_id"] == cid and row["task_id"] == task  # the task comes from the commitment
    g = build_plan(app)["goals"][0]
    assert g["commitments"][0]["open"] == 0 and g["paid"] == 4_500_00

    post(logged_in, f"/commitments/{cid}", what="Parquet offer", amount="4800")
    assert build_plan(app)["goals"][0]["commitments"][0]["open"] == 300_00
    post(logged_in, f"/commitments/{cid}/delete")
    assert build_plan(app)["goals"][0]["commitments"] == []
    assert payments(app)[0]["commitment_id"] is None  # the payment stays


def test_commitment_validation(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    response = post(logged_in, f"/goals/{goal}/commitments", what="", amount="-5")
    assert response.status_code == 400
    assert "What is it for" in response.get_data(as_text=True)


def test_edit_pages_render(logged_in, app, finance):
    goal = add_goal(app, "Doors", "2027-05-01")
    add_commitment(app, goal, "Offer", 1_000_00)
    post(logged_in, f"/goals/{goal}/payments", amount="100", paid_on="2026-11-17", log="1")
    with app.app_context():
        cid = get_db().execute("SELECT id FROM commitments").fetchone()[0]
    assert logged_in.get(f"/commitments/{cid}").status_code == 200
    assert logged_in.get(f"/payments/{payments(app)[0]['id']}").status_code == 200
    assert logged_in.get("/payments/999").status_code == 404
