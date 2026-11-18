"""Big items waiting for big items and tasks, and payments linked from finance."""
import sqlite3
from datetime import date

import pytest
from conftest import TODAY, add_goal, add_task, build_plan, csrf_from, make_app

from renovation import ledger, plan
from renovation.db import get_db


def d(s):
    return date.fromisoformat(s)


def link(app, goal_id=None, task_id=None, needs_goal_id=None, needs_task_id=None):
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO item_deps (goal_id, task_id, needs_goal_id, needs_task_id) VALUES (?, ?, ?, ?)",
                   (goal_id, task_id, needs_goal_id, needs_task_id))
        db.commit()


def post(client, path, **data):
    data.setdefault("csrf", csrf_from(client.get("/")))
    return client.post(path, data=data)


# ── scheduling ──

def test_big_item_waits_for_another_big_item(app):
    kitchen = add_goal(app, "Kitchen", "2027-03-01")
    floor = add_goal(app, "Floor", "2027-02-01")
    pipes = add_task(app, floor, "Pipes", days=5)
    screed = add_task(app, floor, "Screed", days=10, needs=(pipes,))
    cabinets = add_task(app, kitchen, "Cabinets", days=3)
    worktop = add_task(app, kitchen, "Worktop", days=2)
    link(app, goal_id=kitchen, needs_goal_id=floor)
    current = build_plan(app)
    tasks = current["tasks"]
    assert tasks[screed]["end"] == d("2026-12-02")
    # Every kitchen task starts after the whole floor is done.
    assert tasks[cabinets]["start"] == tasks[worktop]["start"] == d("2026-12-03")
    assert tasks[cabinets]["waiting_for"] == ["Floor (all of it)"]
    assert tasks[cabinets]["needs"] == []  # direct task links only
    # The kitchen's deadline reaches back into the floor.
    assert tasks[screed]["latest_end"] == d("2027-02-01")  # its own deadline is earlier
    kitchen_goal = next(g for g in current["goals"] if g["id"] == kitchen)
    assert [x["title"] for x in kitchen_goal["item_needs"]] == ["Floor (all of it)"]


def test_big_item_waits_for_one_task_and_task_waits_for_big_item(app):
    walls = add_goal(app, "Walls", "2027-05-01")
    doors = add_goal(app, "Doors", "2027-05-01")
    paint = add_goal(app, "Paint", "2027-05-01")
    plaster = add_task(app, walls, "Plaster", days=4)
    add_task(app, walls, "Sand", days=20)
    frames = add_task(app, doors, "Frames", days=2)
    brush = add_task(app, paint, "Brush", days=3)
    link(app, goal_id=doors, needs_task_id=plaster)      # doors only need the plaster, not the sanding
    link(app, task_id=brush, needs_goal_id=walls)        # painting needs all of the walls
    tasks = build_plan(app)["tasks"]
    assert tasks[frames]["start"] == d("2026-11-22")
    assert tasks[brush]["start"] == d("2026-12-08")
    assert tasks[brush]["waiting_for"] == ["Walls (all of it)"]


def test_waiting_for_a_big_item_without_tasks_holds_nothing_up(app):
    empty = add_goal(app, "Later", "2027-05-01")
    other = add_goal(app, "Now", "2027-05-01")
    task = add_task(app, other, "Work")
    link(app, goal_id=other, needs_goal_id=empty)
    assert build_plan(app)["tasks"][task]["start"] == TODAY


@pytest.mark.parametrize("case", ["goal-goal", "goal-self", "own-task", "task-own-goal", "empty-goals", "via-task"])
def test_loops_are_refused(app, case):
    a = add_goal(app, "A", "2027-05-01")
    b = add_goal(app, "B", "2027-05-01")
    with app.app_context():
        db = get_db()
        if case == "goal-goal":
            ta, tb = add_task(app, a, "a1"), add_task(app, b, "b1")
            link(app, goal_id=a, needs_goal_id=b)
            assert plan.creates_cycle(db, item_deps=[{"goal_id": b, "needs_goal_id": a}])
        elif case == "goal-self":
            assert plan.creates_cycle(db, item_deps=[{"goal_id": a, "needs_goal_id": a}])
        elif case == "own-task":
            ta = add_task(app, a, "a1")
            assert plan.creates_cycle(db, item_deps=[{"goal_id": a, "needs_task_id": ta}])
        elif case == "task-own-goal":
            ta = add_task(app, a, "a1")
            assert plan.creates_cycle(db, item_deps=[{"task_id": ta, "needs_goal_id": a}])
        elif case == "empty-goals":  # no tasks yet, but they'd wait for each other once they have some
            link(app, goal_id=a, needs_goal_id=b)
            assert plan.creates_cycle(db, item_deps=[{"goal_id": b, "needs_goal_id": a}])
        else:
            ta, tb = add_task(app, a, "a1"), add_task(app, b, "b1")
            link(app, goal_id=b, needs_task_id=ta)       # B waits for a1
            assert plan.would_cycle(db, ta, tb)           # a1 waiting for b1 would loop
            assert not plan.would_cycle(db, tb, ta)


# ── forms and chart ──

def test_goal_form_saves_and_refuses_loops(logged_in, app):
    floor = add_goal(app, "Floor", "2027-02-01")
    kitchen = add_goal(app, "Kitchen", "2027-03-01")
    pipes = add_task(app, floor, "Pipes")
    page = logged_in.get(f"/goals/{kitchen}").get_data(as_text=True)
    assert f'name="needs_goal" value="{floor}"' in page and f'name="needs_goal" value="{kitchen}"' not in page
    response = post(logged_in, f"/goals/{kitchen}", title="Kitchen", deadline="2027-03-01",
                    needs_goal=[str(floor)], needs=[str(pipes)])
    assert response.status_code == 302
    with app.app_context():
        rows = [tuple(r) for r in get_db().execute(
            "SELECT goal_id, needs_goal_id, needs_task_id FROM item_deps ORDER BY id")]
    assert rows == [(kitchen, floor, None), (kitchen, None, pipes)]
    # The floor can't now wait for the kitchen.
    response = post(logged_in, f"/goals/{floor}", title="Floor", deadline="2027-02-01", needs_goal=[str(kitchen)])
    assert response.status_code == 400 and "already waits for this one" in response.get_data(as_text=True)
    assert f'name="needs_goal" value="{kitchen}"' not in logged_in.get(f"/goals/{floor}").get_data(as_text=True)


def test_task_form_waits_for_whole_big_item(logged_in, app):
    walls = add_goal(app, "Walls", "2027-02-01")
    paint = add_goal(app, "Paint", "2027-03-01")
    add_task(app, walls, "Plaster", days=4)
    brush = add_task(app, paint, "Brush")
    page = logged_in.get(f"/tasks/{brush}").get_data(as_text=True)
    assert f'name="needs_goal" value="{walls}"' in page and f'name="needs_goal" value="{paint}"' not in page
    response = post(logged_in, f"/tasks/{brush}", title="Brush", goal_id=str(paint), days="1", status="todo",
                    needs_goal=[str(walls)])
    assert response.status_code == 302
    assert build_plan(app)["tasks"][brush]["start"] == d("2026-11-22")
    response = post(logged_in, f"/tasks/{brush}", title="Brush", goal_id=str(paint), days="1", status="todo",
                    needs_goal=[str(paint)])
    assert response.status_code == 400 and "part of it" in response.get_data(as_text=True)


def test_chart_api_item_links(logged_in, app):
    walls = add_goal(app, "Walls", "2027-02-01")
    doors = add_goal(app, "Doors", "2027-03-01")
    plaster = add_task(app, walls, "Plaster", days=4)
    frames = add_task(app, doors, "Frames")
    token = csrf_from(logged_in.get("/"))
    body = logged_in.post("/api/item-deps", json={"goal_id": doors, "needs_task_id": plaster},
                          headers={"X-CSRF-Token": token}).get_json()
    goal = next(g for g in body["goals"] if g["id"] == doors)
    assert [(x["goal"], x["task"]) for x in goal["item_needs"]] == [(None, plaster)]
    assert next(t for t in body["tasks"] if t["id"] == frames)["start"] == "2026-11-22"
    response = logged_in.post("/api/item-deps", json={"goal_id": walls, "needs_goal_id": doors},
                              headers={"X-CSRF-Token": token})
    assert response.status_code == 400 and "loop" in response.get_json()["error"]
    for bad in ({"goal_id": doors}, {"task_id": frames, "needs_task_id": plaster}, {"goal_id": "x", "needs_goal_id": 1}):
        assert logged_in.post("/api/item-deps", json=bad, headers={"X-CSRF-Token": token}).status_code == 400
    body = logged_in.post(f"/api/item-deps/{goal['item_needs'][0]['id']}/delete", json={},
                          headers={"X-CSRF-Token": token}).get_json()
    assert next(t for t in body["tasks"] if t["id"] == frames)["start"] == "2026-11-18"


# ── linking payments from finance ──

FINANCE_ROWS = [
    {"id": 11, "occurred_at": "2026-10-05 10:00:00", "amount_cents": 3_000_00, "comment": "Kitchen deposit",
     "user_name": "Ana Novak", "off_balance": False, "api_key": None},
    {"id": 12, "occurred_at": "2026-10-20 10:00:00", "amount_cents": 1_200_00, "comment": "Parquet samples",
     "user_name": "Ben Novak", "off_balance": False, "api_key": None},
    {"id": 13, "occurred_at": "2026-11-10 12:00:00", "amount_cents": 500_00, "comment": "Renovation · Doors",
     "user_name": "Ben Novak", "off_balance": False, "api_key": "renovation-payment-x"},
]


@pytest.fixture
def finance_list(monkeypatch):
    rows = [dict(r) for r in FINANCE_ROWS]

    class Response:
        status_code = 200

        def json(self):
            return {"category": "hisa", "transactions": rows}

    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append((url, params, headers))
        return Response()

    monkeypatch.setattr(ledger.requests, "get", fake_get)
    return rows, calls


def payments(app):
    with app.app_context():
        return [dict(r) for r in get_db().execute("SELECT * FROM payments ORDER BY id")]


def test_link_payments_from_finance(logged_in, app, finance_list):
    rows, calls = finance_list
    kitchen = add_goal(app, "Kitchen", "2027-03-01")
    cabinets = add_task(app, kitchen, "Cabinets", cost=8_000_00)
    page = logged_in.get(f"/goals/{kitchen}/link").get_data(as_text=True)
    assert calls[-1] == ("http://finance:8000/api/v1/transactions", {"category": "hisa"},
                         {"Authorization": "Bearer api-token"})
    assert "Kitchen deposit" in page and "Parquet samples" in page
    assert "Renovation · Doors" not in page  # logged from the planner: not offered
    response = post(logged_in, f"/goals/{kitchen}/link", tx=["11", "999"], task_id=str(cabinets))
    assert response.status_code == 302
    linked = payments(app)
    assert len(linked) == 1
    p = linked[0]
    assert (p["finance"], p["finance_tx_id"], p["amount_cents"], p["paid_on"], p["what"], p["task_id"],
            p["user_name"]) == ("linked", 11, 3_000_00, "2026-10-05", "Kitchen deposit", cabinets, "Ana Novak")
    # Already linked: not offered again, for any big item.
    other = add_goal(app, "Parquet", "2027-03-01")
    assert "Kitchen deposit" not in logged_in.get(f"/goals/{other}/link").get_data(as_text=True)
    goal = build_plan(app)["goals"][0]
    assert goal["paid"] == 3_000_00
    assert "from finance" in logged_in.get(f"/goals/{kitchen}").get_data(as_text=True)


def test_linked_payment_follows_finance_and_unlinks_without_deleting(logged_in, app, finance_list, monkeypatch):
    rows, _ = finance_list
    kitchen = add_goal(app, "Kitchen", "2027-03-01")
    post(logged_in, f"/goals/{kitchen}/link", tx=["11"])
    pid = payments(app)[0]["id"]
    # Changed in finance: the link page brings it up to date.
    rows[0]["amount_cents"] = 3_100_00
    logged_in.get(f"/goals/{kitchen}/link")
    assert payments(app)[0]["amount_cents"] == 3_100_00
    # Editing here changes only what it's for; amount and day stay finance's.
    post(logged_in, f"/payments/{pid}", amount="1", paid_on="2020-01-01", what="Deposit, kitchen")
    p = payments(app)[0]
    assert (p["amount_cents"], p["paid_on"], p["what"], p["finance"]) == (3_100_00, "2026-10-05", "Deposit, kitchen", "linked")
    # Unlinking never calls finance.
    monkeypatch.setattr(ledger.requests, "request", lambda *a, **k: pytest.fail("finance was called"))
    post(logged_in, f"/payments/{pid}/delete")
    assert payments(app) == []
    # Gone from finance: flagged on the link page.
    post(logged_in, f"/goals/{kitchen}/link", tx=["12"])
    rows.pop(1)
    page = logged_in.get(f"/goals/{kitchen}/link").get_data(as_text=True)
    assert "no longer is in finance" in page


def test_link_page_when_finance_is_down(logged_in, app, monkeypatch):
    import requests

    def down(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(ledger.requests, "get", down)
    kitchen = add_goal(app, "Kitchen", "2027-03-01")
    response = logged_in.get(f"/goals/{kitchen}/link")
    assert response.status_code == 302
    assert "didn" in logged_in.get(f"/goals/{kitchen}").get_data(as_text=True)


# ── migration ──

def test_migration_3_keeps_payments(tmp_path):
    app = make_app(tmp_path)  # creates the current schema...
    path = tmp_path / "renovation.db"
    conn = sqlite3.connect(path)
    # ...turn it back into version 2: payments without the linked state.
    conn.executescript("""
        DROP TABLE item_deps;
        DROP TABLE payments;
        CREATE TABLE payments (id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            commitment_id INTEGER REFERENCES commitments(id) ON DELETE SET NULL, paid_on TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (amount_cents <> 0), what TEXT NOT NULL DEFAULT '',
            off_balance INTEGER NOT NULL DEFAULT 0,
            finance TEXT NOT NULL DEFAULT 'skip' CHECK (finance IN ('logged', 'failed', 'skip')),
            finance_key TEXT UNIQUE, finance_error TEXT, user_name TEXT, created_at TEXT NOT NULL);
        INSERT INTO goals (id, title, deadline, created_at) VALUES (1, 'Doors', '2027-05-01', 'x');
        INSERT INTO payments (goal_id, paid_on, amount_cents, finance, finance_key, created_at)
            VALUES (1, '2026-11-01', 500, 'logged', 'k1', 'x');
        PRAGMA user_version = 2;
    """)
    conn.close()
    app = make_app(tmp_path)
    with app.app_context():
        db = get_db()
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        row = dict(db.execute("SELECT * FROM payments").fetchone())
        assert (row["amount_cents"], row["finance"], row["finance_key"], row["finance_tx_id"]) == (500, "logged", "k1", None)
        db.execute("INSERT INTO payments (goal_id, paid_on, amount_cents, finance, finance_tx_id, created_at) "
                   "VALUES (1, '2026-11-02', 100, 'linked', 7, 'x')")
        db.execute("INSERT INTO item_deps (goal_id, needs_goal_id) VALUES (1, 1)")
