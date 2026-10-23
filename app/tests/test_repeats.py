from datetime import date

from finance import repeats
from finance.db import get_db


def add_repeat(app, day, active_from="2026-01-01", cents=1989, category="tel"):
    with app.app_context():
        db = get_db()
        cur = db.execute(
            """INSERT INTO repeats (user_name, amount_cents, category, comment, is_income, day_of_month,
                   active_from, created_at) VALUES ('Ana', ?, ?, 'phone', 0, ?, ?, '2026-01-01 00:00:00')""",
            (cents, category, day, active_from),
        )
        db.commit()
        return cur.lastrowid


def run(app, today):
    with app.app_context():
        db = get_db()
        repeats.run_due(db, today)
        return [r["occurred_at"] for r in db.execute("SELECT occurred_at FROM transactions ORDER BY occurred_at")]


def test_due_date_clamps_to_month_end():
    assert repeats.due_date(2026, 2, 31) == date(2026, 2, 28)
    assert repeats.due_date(2028, 2, 30) == date(2028, 2, 29)
    assert repeats.due_date(2026, 4, 31) == date(2026, 4, 30)


def test_logs_due_entries_and_catches_up_last_month(app):
    add_repeat(app, 18)
    assert run(app, date(2026, 3, 20)) == ["2026-02-18 03:00:00", "2026-03-18 03:00:00"]


def test_running_again_logs_nothing_new(app):
    add_repeat(app, 18)
    run(app, date(2026, 3, 20))
    assert len(run(app, date(2026, 3, 21))) == 2


def test_not_due_yet(app):
    add_repeat(app, 25)
    assert run(app, date(2026, 3, 20)) == ["2026-02-25 03:00:00"]


def test_respects_active_from(app):
    add_repeat(app, 18, active_from="2026-03-19")
    assert run(app, date(2026, 3, 20)) == []
    assert run(app, date(2026, 4, 18)) == ["2026-04-18 03:00:00"]


def test_deleted_entry_is_not_logged_again(app):
    add_repeat(app, 18)
    run(app, date(2026, 3, 20))
    with app.app_context():
        db = get_db()
        db.execute("DELETE FROM transactions")
        db.commit()
    assert run(app, date(2026, 3, 21)) == []


def test_day_31_logs_on_last_day_of_short_month(app):
    add_repeat(app, 31, active_from="2026-04-01")
    assert run(app, date(2026, 4, 30)) == ["2026-04-30 03:00:00"]


def test_uses_category_aliases(app):
    add_repeat(app, 1, category="Zavarovanje")
    with app.app_context():
        run(app, date(2026, 1, 1))
        assert get_db().execute("SELECT category FROM transactions").fetchone()[0] == "insurance"
