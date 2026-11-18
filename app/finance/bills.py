"""Monthly bills: electricity, water, internet. The amount changes every
month, so they can't be repeats. Instead, categories ticked as bills are
expected once a month, and on the reminder days (BILL_REMINDER_DAYS, default
10, 15 and 20) the bot lists the ones not logged yet.

A bill counts for the month it was paid in. "elektrika 0" marks a month
without one.
"""
from flask import current_app

from . import telegram
from .util import fmt_eur, fmt_month, month_bounds, month_key, plain_amount, shift_month, ts

REMINDER_HOUR = 9  # the scheduler sends the day's reminder from this hour on
SENT_KEY = "bill_reminder_sent"  # settings row: the date of the last reminder


def status(db, ym):
    """Every bill category with what was logged for it in month `ym`."""
    start, end = month_bounds(ym)
    return db.execute(
        """SELECT c.key, c.label, COUNT(t.id) AS n, COALESCE(SUM(t.amount_cents), 0) AS total
           FROM categories c
           LEFT JOIN transactions t ON t.category = c.key AND NOT t.is_income
                AND t.occurred_at >= ? AND t.occurred_at < ?
           WHERE c.bill = 1 GROUP BY c.key ORDER BY c.label COLLATE NOCASE""",
        (start, end),
    ).fetchall()


def missing(db, ym):
    return [row for row in status(db, ym) if not row["n"]]


def _example(db, key):
    """'elektrika 128.94': the bill with the amount last paid, as a model message."""
    row = db.execute(
        """SELECT amount_cents FROM transactions WHERE category = ? AND NOT is_income AND amount_cents > 0
           ORDER BY occurred_at DESC LIMIT 1""",
        (key,),
    ).fetchone()
    return f"{key} {plain_amount(row['amount_cents']) if row else '50.00'}"


def _month_lines(rows):
    return [f"✓ {r['label']} {fmt_eur(r['total'])}" if r["n"] else f"✗ {r['label']}" for r in rows]


def status_text(db, today):
    """This month and last month, for the "bills" command."""
    this_month = month_key(today)
    current = status(db, this_month)
    if not current:
        return 'No category is marked as a monthly bill yet. Tick "Bill" on the Categories page.'
    previous = shift_month(this_month, -1)
    return "\n".join(
        [f"Bills, {fmt_month(this_month)}:", *_month_lines(current), "",
         f"{fmt_month(previous)}:", *_month_lines(status(db, previous)), "",
         f'Log one like "{_example(db, current[0]["key"])}", or "{current[0]["key"]} 0" if there is none this month.']
    )


def reminder_text(db, today):
    """The reminder for today, or None when every bill is in."""
    ym = month_key(today)
    rows = missing(db, ym)
    if not rows:
        return None
    names = ", ".join(r["label"] for r in rows)
    key = rows[0]["key"]
    return (f"Bills for {fmt_month(ym)} not logged yet: {names}.\n"
            f'Log each once it is paid, e.g. "{_example(db, key)}", or "{key} 0" if there is none this month.\n'
            'Send "bills" to see this month and last.')


def reminder_days():
    raw = current_app.config.get("BILL_REMINDER_DAYS", "10,15,20")
    return {int(part) for part in str(raw).replace(" ", "").split(",") if part.isdigit()}


def reminder_chat():
    """BILL_REMINDER_CHAT, else the household group (the allowed chat with a
    negative id), else the admin."""
    cfg = current_app.config
    if cfg.get("BILL_REMINDER_CHAT"):
        return int(cfg["BILL_REMINDER_CHAT"])
    groups = sorted(chat for chat in cfg["TELEGRAM_ALLOWED_IDS"] if chat < 0)
    if groups:
        return groups[0]
    return int(cfg["TELEGRAM_ADMIN_ID"]) if cfg["TELEGRAM_ADMIN_ID"] else None


def remind(db, moment):
    """Send today's reminder if today is a reminder day and it hasn't gone
    out yet. Returns the text sent, or None."""
    today = moment.date()
    if today.day not in reminder_days() or moment.hour < REMINDER_HOUR:
        return None
    sent = db.execute("SELECT value FROM settings WHERE key = ?", (SENT_KEY,)).fetchone()
    if sent and sent["value"] == today.isoformat():
        return None
    db.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (SENT_KEY, today.isoformat(), ts(moment)),
    )
    db.commit()
    text = reminder_text(db, today)
    chat = reminder_chat()
    if text and chat is not None:
        telegram.send_text(chat, text)
        return text
    return None
