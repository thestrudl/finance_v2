"""Monthly repeat entries (the old logRepeatData trigger).

A repeat due on day 31 is logged on the last day of shorter months. If the
scheduler was down, entries due earlier this month or last month are logged
when it comes back; repeat_runs makes every run safe to repeat.
"""
import calendar
from datetime import date

from . import categories
from .util import now, ts


def due_date(year, month, day):
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _previous_month(today):
    return (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)


def run_due(db, today):
    """Log every repeat due on or before `today`; returns the new transaction ids."""
    created = []
    stamp = ts(now())
    for rep in db.execute("SELECT * FROM repeats ORDER BY id").fetchall():
        active_from = date.fromisoformat(rep["active_from"])
        for year, month in (_previous_month(today), (today.year, today.month)):
            due = due_date(year, month, rep["day_of_month"])
            if due > today or due < active_from:
                continue
            claimed = db.execute(
                "INSERT OR IGNORE INTO repeat_runs (repeat_id, due_date) VALUES (?, ?)",
                (rep["id"], due.isoformat()),
            )
            if claimed.rowcount == 0:
                continue
            cur = db.execute(
                """INSERT INTO transactions (occurred_at, user_name, amount_cents, category,
                       category_raw, comment, is_income, source, repeat_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'repeat', ?, ?)""",
                (
                    f"{due.isoformat()} 03:00:00",
                    rep["user_name"],
                    rep["amount_cents"],
                    categories.resolve(db, rep["category"]),
                    rep["category"],
                    rep["comment"],
                    rep["is_income"],
                    rep["id"],
                    stamp,
                ),
            )
            created.append(cur.lastrowid)
    db.commit()
    return created
