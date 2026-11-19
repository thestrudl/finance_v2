import re

import pytest
from conftest import ANA, add_goal, add_task, csrf_from, login, make_app

from renovation.db import get_db

PAGES = ["/", "/upcoming", "/money", "/goals/new", "/tasks/new", "/api/plan"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_need_login(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_by_emailed_code(client, outbox):
    response = login(client, outbox)
    assert response.status_code == 302
    assert outbox[-1]["Subject"].startswith("Renovation login code: ")
    assert client.get("/").status_code == 200


def test_renovation_users_override_portal_users(tmp_path, monkeypatch, outbox):
    monkeypatch.setenv("RENOVATION_USERS", "Ana <ana@example.com>")
    monkeypatch.setenv("PORTAL_USERS", "Ben <ben@example.com>")
    from renovation import load_config

    assert load_config()["PORTAL_USERS"] == [("Ana", ANA)]
    monkeypatch.setenv("RENOVATION_USERS", "")
    assert load_config()["PORTAL_USERS"] == [("Ben", "ben@example.com")]


def test_missing_secret_key_refuses_to_start(tmp_path):
    with pytest.raises(RuntimeError):
        make_app(tmp_path, SECRET_KEY="")


def test_pages_render(logged_in, app):
    goal = add_goal(app, "Doors <2nd floor>", "2027-05-01", budget=3_000_00)
    walls = add_task(app, goal, "Walls", days=10, cost=1_000_00)
    add_task(app, goal, "Parquet", days=4, needs=(walls,))
    for path in ["/", "/upcoming", f"/goals/{goal}", f"/tasks/{walls}", "/tasks/new", f"/tasks/new?goal={goal}"]:
        response = logged_in.get(path)
        assert response.status_code == 200, path
        assert "Doors &lt;2nd floor&gt;" in response.get_data(as_text=True)
    page = logged_in.get("/").get_data(as_text=True)
    assert 'id="plan-data"' in page and "gantt.js" in page


def test_security_headers(logged_in):
    response = logged_in.get("/")
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"


def test_add_goal_needs_a_deadline(logged_in, app):
    token = csrf_from(logged_in.get("/goals/new"))
    response = logged_in.post("/goals/new", data={"csrf": token, "title": "Doors", "deadline": ""})
    assert response.status_code == 400
    assert "needs a deadline" in response.get_data(as_text=True)
    response = logged_in.post("/goals/new", data={"csrf": token, "title": "Doors", "deadline": "2027-05-01",
                                                  "budget": "1.500,50", "next": "/"})
    assert response.status_code == 302
    with app.app_context():
        row = get_db().execute("SELECT * FROM goals").fetchone()
    assert (row["title"], row["deadline"], row["budget_cents"]) == ("Doors", "2027-05-01", 1_500_50)


def test_forms_need_csrf(logged_in):
    response = logged_in.post("/goals/new", data={"title": "Doors", "deadline": "2027-05-01"})
    assert response.status_code == 400


def test_add_and_edit_task(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    walls = add_task(app, goal, "Walls", days=10)
    token = csrf_from(logged_in.get("/tasks/new"))
    response = logged_in.post("/tasks/new", data={
        "csrf": token, "title": "Drywall", "goal_id": goal, "days": "7", "cost": "2400", "paid": "",
        "pay_at": "end", "needs": [str(walls)], "who": "Marko", "status": "todo", "next": "/",
    })
    assert response.status_code == 302
    with app.app_context():
        db = get_db()
        task = db.execute("SELECT * FROM tasks WHERE title = 'Drywall'").fetchone()
        assert (task["days"], task["cost_cents"], task["pay_at"], task["who"]) == (7, 2_400_00, "end", "Marko")
        assert db.execute("SELECT needs_id FROM task_deps WHERE task_id = ?", (task["id"],)).fetchone()[0] == walls

    # Walls can't wait for drywall: drywall already waits for the walls.
    response = logged_in.post(f"/tasks/{walls}", data={
        "csrf": token, "title": "Walls", "goal_id": goal, "days": "10", "needs": [str(task["id"])], "status": "todo",
    })
    assert response.status_code == 400
    assert "already waits for this one" in response.get_data(as_text=True)

    response = logged_in.post(f"/tasks/{walls}", data={
        "csrf": token, "title": "Walls", "goal_id": goal, "days": "12", "status": "doing", "next": "/upcoming",
    })
    assert response.headers["Location"].endswith("/upcoming")
    with app.app_context():
        row = get_db().execute("SELECT * FROM tasks WHERE id = ?", (walls,)).fetchone()
    assert (row["days"], row["status"], row["started_on"]) == (12, "doing", "2026-11-18")


def test_task_form_hides_tasks_that_wait_for_it(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    walls = add_task(app, goal, "Walls")
    drywall = add_task(app, goal, "Drywall", needs=(walls,))
    add_task(app, goal, "Parquet", needs=(drywall,))
    page = logged_in.get(f"/tasks/{walls}").get_data(as_text=True)
    assert not re.search(r'name="needs" value="\d+"[^>]*>\s*Drywall', page)
    assert not re.search(r'name="needs" value="\d+"[^>]*>\s*Parquet', page)


def test_bad_task_input(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    token = csrf_from(logged_in.get("/tasks/new"))
    response = logged_in.post("/tasks/new", data={
        "csrf": token, "title": "", "goal_id": goal, "days": "0", "cost": "lots", "not_before": "soon",
    })
    assert response.status_code == 400
    text = response.get_data(as_text=True)
    for message in ("Give it a name", "Days is a whole number", "The estimate isn", "Not before isn"):
        assert message in text


def test_quick_status(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    task = add_task(app, goal, "Fitter", cost=800_00)
    token = csrf_from(logged_in.get("/upcoming"))
    logged_in.post(f"/tasks/{task}/status", data={"csrf": token, "status": "doing"})
    logged_in.post(f"/tasks/{task}/status", data={"csrf": token, "status": "done"})
    with app.app_context():
        row = get_db().execute("SELECT * FROM tasks WHERE id = ?", (task,)).fetchone()
    assert (row["status"], row["started_on"], row["done_on"]) == ("done", "2026-11-18", "2026-11-18")
    with app.app_context():  # finishing a task doesn't invent a payment
        assert get_db().execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
    logged_in.post(f"/tasks/{task}/status", data={"csrf": token, "status": "todo"})
    with app.app_context():
        row = get_db().execute("SELECT * FROM tasks WHERE id = ?", (task,)).fetchone()
    assert (row["status"], row["started_on"], row["done_on"]) == ("todo", None, None)


def test_delete_goal_takes_its_tasks(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    other = add_goal(app, "Roof", "2027-05-01")
    task = add_task(app, goal, "Walls")
    add_task(app, other, "Tiles", needs=(task,))
    token = csrf_from(logged_in.get("/"))
    logged_in.post(f"/goals/{goal}/delete", data={"csrf": token})
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM task_deps").fetchone()[0] == 0


# ── chart JSON ──

def api(client, path, body, token):
    return client.post(path, json=body, headers={"X-CSRF-Token": token})


def test_api_needs_csrf_header(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    task = add_task(app, goal, "Walls")
    assert logged_in.post(f"/api/tasks/{task}/resize", json={"days": 3}).status_code == 400
    assert api(logged_in, f"/api/tasks/{task}/resize", {"days": 3}, "wrong").status_code == 400


def test_api_move_resize_link_deadline(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    walls = add_task(app, goal, "Walls", days=5)
    drywall = add_task(app, goal, "Drywall", days=3)
    token = csrf_from(logged_in.get("/"))

    body = api(logged_in, f"/api/tasks/{walls}/move", {"start": "2026-12-01"}, token).get_json()
    by_id = {t["id"]: t for t in body["tasks"]}
    assert by_id[walls]["start"] == "2026-12-01" and by_id[walls]["not_before"] == "2026-12-01"

    body = api(logged_in, f"/api/tasks/{walls}/resize", {"days": 8}, token).get_json()
    assert {t["id"]: t for t in body["tasks"]}[walls]["end"] == "2026-12-08"

    body = api(logged_in, f"/api/tasks/{drywall}/needs", {"needs_id": walls}, token).get_json()
    assert {t["id"]: t for t in body["tasks"]}[drywall]["start"] == "2026-12-09"

    response = api(logged_in, f"/api/tasks/{walls}/needs", {"needs_id": drywall}, token)
    assert response.status_code == 400 and "loop" in response.get_json()["error"]

    body = api(logged_in, f"/api/tasks/{drywall}/needs", {"needs_id": walls, "remove": True}, token).get_json()
    assert {t["id"]: t for t in body["tasks"]}[drywall]["start"] == "2026-11-18"

    body = api(logged_in, f"/api/goals/{goal}/deadline", {"deadline": "2027-06-01"}, token).get_json()
    assert body["goals"][0]["deadline"] == "2027-06-01"

    # Dragging back into the past clears "not before": it starts as soon as it can.
    body = api(logged_in, f"/api/tasks/{walls}/move", {"start": "2026-11-01"}, token).get_json()
    assert {t["id"]: t for t in body["tasks"]}[walls]["not_before"] is None


def test_api_refuses_bad_moves(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    done = add_task(app, goal, "Old", status="done", done_on="2026-11-01")
    doing = add_task(app, goal, "Now", status="doing", started_on="2026-11-15")
    token = csrf_from(logged_in.get("/"))
    assert api(logged_in, f"/api/tasks/{done}/move", {"start": "2026-12-01"}, token).status_code == 400
    assert api(logged_in, f"/api/tasks/{doing}/move", {"start": "2026-12-01"}, token).status_code == 400
    assert api(logged_in, f"/api/tasks/{doing}/move", {"start": "2026-11-10"}, token).status_code == 200
    assert api(logged_in, f"/api/tasks/{doing}/resize", {"days": 0}, token).status_code == 400
    assert api(logged_in, f"/api/tasks/{doing}/move", {"start": "nope"}, token).status_code == 400
    assert api(logged_in, "/api/tasks/999/move", {"start": "2026-12-01"}, token).status_code == 404


def test_static_assets_are_local(logged_in):
    page = logged_in.get("/").get_data(as_text=True)
    assert "https://" not in re.sub(r'href="https://blog\.rokvidmar\.xyz/"', "", page)
    assert logged_in.get("/static/gantt.js").status_code == 200
    assert logged_in.get("/static/fonts/fonts.css").status_code == 200


def test_healthz(client):
    assert client.get("/healthz").get_data(as_text=True) == "ok"


def test_notes_are_shown_with_their_line_breaks(logged_in, app):
    goal = add_goal(app, "Doors", "2027-05-01")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE goals SET notes = ? WHERE id = ?", ("Oak, 80 cm\nOffer from Jelovica: €2,800", goal))
        db.commit()
    task = add_task(app, goal, "Parquet")
    token = csrf_from(logged_in.get("/"))
    logged_in.post(f"/tasks/{task}", data={"csrf": token, "title": "Parquet", "goal_id": goal, "days": "4",
                                           "status": "todo", "notes": "Call Marko first <b>\nBring 3 packs extra"})
    page = logged_in.get("/upcoming").get_data(as_text=True)
    assert "Call Marko first &lt;b&gt;\nBring 3 packs extra" in page
    assert "Oak, 80 cm\nOffer from Jelovica" in page
    assert "Bring 3 packs extra" in logged_in.get(f"/goals/{goal}").get_data(as_text=True)
    body = logged_in.get("/api/plan").get_json()
    assert body["tasks"][0]["notes"] == "Call Marko first <b>\nBring 3 packs extra"
    assert body["goals"][0]["notes"].startswith("Oak, 80 cm")
