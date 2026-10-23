import pytest
from conftest import add_tx, csrf_from, login, message_update, post_update

from finance.db import get_db
from finance.util import month_key, now, shift_month


@pytest.fixture
def logged_in(client, outbox):
    assert login(client, outbox).status_code == 302
    return client


@pytest.fixture
def data(app):
    with app.app_context():
        ym = month_key(now())
    add_tx(app, f"{ym}-01 10:00:00", 4000_00, "placa", is_income=True)
    add_tx(app, f"{ym}-01 12:00:00", 12_50, "Food", comment="spar")
    add_tx(app, f"{ym}-01 13:00:00", 900_00, "Food", comment="big party")
    add_tx(app, f"{ym}-01 14:00:00", 300_00, "sunny", comment="parts")
    add_tx(app, f"{ym}-01 15:00:00", 500_00, "sunny", is_income=True)
    add_tx(app, f"{shift_month(ym, -4)}-03 09:00:00", 45_00, "Hrana")
    for day in range(1, 7):
        add_tx(app, f"{shift_month(ym, -1)}-0{day} 09:00:00", 20_00, "food")
    return ym


def test_pages_need_login(client):
    for path in ("/", "/year", "/transactions", "/categories", "/repeats", "/category/food"):
        response = client.get(path)
        assert response.status_code == 302 and "/login" in response.headers["Location"], path


def test_health_check_is_public(client):
    assert client.get("/healthz").status_code == 200


def test_post_without_csrf_token_is_rejected(logged_in):
    assert logged_in.post("/repeats", data={"category": "x", "amount": "1", "day": "1"}).status_code == 400


def test_every_page_renders(app, logged_in, data):
    with app.app_context():
        tx_id = get_db().execute("SELECT id FROM transactions LIMIT 1").fetchone()[0]
    for path in ("/", f"/?month={data}&scope=core", "/year", "/year?year=2025", "/transactions",
                 f"/transactions?month={data}&category=food&type=expense&q=spar", "/categories",
                 "/repeats", "/category/food", "/category/sunny", f"/transactions/{tx_id}/edit"):
        assert logged_in.get(path).status_code == 200, path


def test_overview_shows_categories_and_business(logged_in, data):
    html = logged_in.get("/").get_data(as_text=True)
    assert "Food" in html and "Sunny" in html
    assert "€4,000.00" in html  # income tile
    assert "big party" in html  # unusual: 900 against a 20 median


def test_add_edit_delete_transaction(app, logged_in, data):
    token = csrf_from(logged_in.get("/transactions"))
    logged_in.post("/transactions", data={
        "csrf": token, "type": "expense", "category": "Gas", "amount": "61,20",
        "occurred_at": "2026-10-01T08:30", "comment": "petrol",
    })
    with app.app_context():
        row = get_db().execute("SELECT * FROM transactions WHERE comment = 'petrol'").fetchone()
    assert (row["amount_cents"], row["category"], row["occurred_at"], row["source"]) == (6120, "gas", "2026-10-01 08:30:00", "web")

    logged_in.post(f"/transactions/{row['id']}/edit", data={
        "csrf": token, "type": "expense", "category": "car", "amount": "70",
        "occurred_at": "2026-10-01T08:30:00", "comment": "petrol",
    })
    with app.app_context():
        assert get_db().execute("SELECT category, amount_cents FROM transactions WHERE id = ?", (row["id"],)).fetchone()[:] == ("car", 7000)

    logged_in.post(f"/transactions/{row['id']}/delete", data={"csrf": token})
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM transactions WHERE id = ?", (row["id"],)).fetchone()[0] == 0


def test_merge_moves_entries_and_future_messages(app, client, logged_in, data, sent):
    token = csrf_from(logged_in.get("/categories"))
    logged_in.post("/categories/hrana/merge", data={"csrf": token, "target": "food"})
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM transactions WHERE category = 'hrana'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM categories WHERE key = 'hrana'").fetchone()[0] == 0
    post_update(client, message_update("Hrana 3"))
    with app.app_context():
        assert get_db().execute("SELECT category FROM transactions ORDER BY id DESC").fetchone()[0] == "food"


def test_category_budget_and_group(app, logged_in, data):
    token = csrf_from(logged_in.get("/categories"))
    logged_in.post("/categories/food", data={
        "csrf": token, "label": "Food & groceries", "grp": "living", "budget": "500", "budget_period": "month",
        "description": "groceries",
    })
    with app.app_context():
        cat = get_db().execute("SELECT * FROM categories WHERE key = 'food'").fetchone()
    assert (cat["label"], cat["budget_cents"], cat["budget_period"]) == ("Food & groceries", 50000, "month")
    html = logged_in.get("/").get_data(as_text=True)
    assert "Over budget" in html  # 912.50 against 500


def test_add_and_delete_repeat(app, logged_in):
    token = csrf_from(logged_in.get("/repeats"))
    logged_in.post("/repeats", data={"csrf": token, "type": "expense", "category": "tel", "amount": "19.89", "day": "18"})
    with app.app_context():
        rep = get_db().execute("SELECT * FROM repeats").fetchone()
    assert (rep["amount_cents"], rep["day_of_month"]) == (1989, 18)
    logged_in.post(f"/repeats/{rep['id']}/delete", data={"csrf": token})
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM repeats").fetchone()[0] == 0
