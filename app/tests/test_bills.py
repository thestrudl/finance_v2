from datetime import date, datetime

import pytest
from conftest import ADMIN, GROUP_CHAT, add_tx, callback_update, csrf_from, login, message_update, post_update, texts

from finance import bills, categories
from finance.db import get_db


def mark_bills(app, *keys):
    with app.app_context():
        db = get_db()
        for key in keys:
            categories.resolve(db, key)
            db.execute("UPDATE categories SET bill = 1 WHERE key = ?", (key,))
        db.commit()


def remind(app, moment):
    with app.app_context():
        return bills.remind(get_db(), moment)


def test_household_bills_are_marked_with_their_spellings(app):
    household = {"categories": [("elektrika", "Elektrika", "living", "power")], "bills": ["elektrika", "voda"],
                 "aliases": {"struja": "elektrika", "fuel": "gas"}}
    with app.app_context():
        db = get_db()
        categories.apply_bills(db, household)
        rows = {r["key"]: dict(r) for r in db.execute("SELECT * FROM categories WHERE bill = 1")}
        assert set(rows) == {"elektrika", "voda"}
        assert (rows["elektrika"]["label"], rows["elektrika"]["description"]) == ("Elektrika", "power")
        assert categories.lookup(db, "Struja") == "elektrika"


def test_new_household_categories_come_in_without_undoing_merges(app):
    household = {"categories": [("taxes", "Taxes", "business", "s.p."), ("zobar", "Zobar", "living", ""),
                                ("food", "Food", "project", "")],
                 "bills": [], "aliases": {"davki": "taxes", "dentist": "zobar"}}
    with app.app_context():
        db = get_db()
        categories.resolve(db, "zdravje")
        categories.resolve(db, "zobar")
        categories.merge(db, "zobar", "zdravje")
        categories.apply_bills(db, household)
        groups = dict(db.execute("SELECT key, grp FROM categories").fetchall())
        assert groups["taxes"] == "business" and categories.lookup(db, "Davki") == "taxes"
        assert "zobar" not in groups                     # merged away, stays merged
        assert categories.lookup(db, "dentist") == "dentist"  # not pointed at a merged-away key
        assert groups["food"] == "living"                # existing group kept


def test_status_counts_expenses_paid_in_the_month(app):
    mark_bills(app, "elektrika", "voda", "internet")
    add_tx(app, "2026-11-06 10:00:00", 128_94, "elektrika")
    add_tx(app, "2026-11-05 10:00:00", 0, "internet")              # logged as "none this month"
    add_tx(app, "2026-10-19 10:00:00", 32_00, "voda")              # last month
    add_tx(app, "2026-11-07 10:00:00", 5_00, "voda", is_income=True)  # income doesn't count
    with app.app_context():
        db = get_db()
        assert [(r["key"], r["n"], r["total"]) for r in bills.status(db, "2026-11")] == [
            ("elektrika", 1, 128_94), ("internet", 1, 0), ("voda", 0, 0)]
        assert [r["key"] for r in bills.missing(db, "2026-11")] == ["voda"]


def test_reminder_on_reminder_days_once(app, sent):
    mark_bills(app, "elektrika", "voda")
    add_tx(app, "2026-11-06 10:00:00", 128_94, "elektrika")
    assert remind(app, datetime(2026, 11, 10, 8, 50)) is None      # before 9:00
    assert remind(app, datetime(2026, 11, 11, 12, 0)) is None      # not a reminder day
    text = remind(app, datetime(2026, 11, 10, 9, 0))
    assert "Bills for November 2026 not logged yet: Voda." in text
    assert 'e.g. "voda 50.00", or "voda 0"' in text  # never paid yet: a placeholder amount
    assert remind(app, datetime(2026, 11, 10, 15, 0)) is None      # once a day
    assert texts(sent, GROUP_CHAT) == [text]
    assert remind(app, datetime(2026, 11, 15, 9, 10)) == text


def test_no_reminder_when_everything_is_logged(app, sent):
    mark_bills(app, "elektrika")
    add_tx(app, "2026-11-06 10:00:00", 128_94, "elektrika")
    assert remind(app, datetime(2026, 11, 20, 10, 0)) is None
    assert texts(sent) == []


def test_no_reminder_without_bills(app, sent):
    assert remind(app, datetime(2026, 11, 20, 10, 0)) is None
    assert texts(sent) == []


def test_reminder_days_and_chat_are_configurable(app, sent):
    app.config.update(BILL_REMINDER_DAYS="3, 25", BILL_REMINDER_CHAT=str(ADMIN))
    mark_bills(app, "voda")
    assert remind(app, datetime(2026, 11, 10, 10, 0)) is None
    assert remind(app, datetime(2026, 11, 25, 10, 0))
    assert len(texts(sent, ADMIN)) == 1


def test_bills_command(app, client, sent):
    mark_bills(app, "elektrika", "voda")
    with app.app_context():
        from finance.util import month_key, now, shift_month
        this_month = month_key(now().date())
    add_tx(app, f"{this_month}-01 10:00:00", 87_89, "elektrika")
    add_tx(app, f"{shift_month(this_month, -1)}-18 10:00:00", 32_00, "voda")
    post_update(client, message_update("bills"))
    post_update(client, callback_update("bills"))
    reply = texts(sent, GROUP_CHAT)
    assert len(reply) == 2 and reply[0] == reply[1]
    lines = reply[0].split("\n")
    assert lines[1:3] == ["✓ Elektrika €87.89", "✗ Voda"]
    assert lines[5:7] == ["✗ Elektrika", "✓ Voda €32.00"]
    assert 'Log one like "elektrika 87.89"' in lines[-1]  # the amount last paid


def test_bills_command_without_bills(app, client, sent):
    post_update(client, message_update("bills"))
    assert "Tick \"Bill\"" in texts(sent)[0]


@pytest.fixture
def logged_in(client, outbox):
    assert login(client, outbox).status_code == 302
    return client


def test_bill_tick_box_on_categories(app, logged_in):
    add_tx(app, "2026-11-06 10:00:00", 128_94, "elektrika")
    token = csrf_from(logged_in.get("/categories"))
    form = {"csrf": token, "label": "Elektrika", "grp": "living", "budget": "", "description": ""}
    logged_in.post("/categories/elektrika", data={**form, "bill": "1"})
    with app.app_context():
        assert get_db().execute("SELECT bill FROM categories WHERE key = 'elektrika'").fetchone()[0] == 1
    assert 'name="bill" value="1" checked' in logged_in.get("/category/elektrika").get_data(as_text=True)
    logged_in.post("/categories/elektrika", data=form)
    with app.app_context():
        assert get_db().execute("SELECT bill FROM categories WHERE key = 'elektrika'").fetchone()[0] == 0


def test_reminder_date_is_remembered_across_restarts(app, sent):
    mark_bills(app, "voda")
    remind(app, datetime(2026, 11, 10, 9, 0))
    with app.app_context():
        value = get_db().execute("SELECT value FROM settings WHERE key = ?", (bills.SENT_KEY,)).fetchone()[0]
    assert value == date(2026, 11, 10).isoformat()
