"""Bank & cash, estimated: the latest balance check plus income minus spending
logged since. A transaction marked off_balance (paid or received outside our
accounts, like a gift paid straight to the seller) doesn't move the estimate.

Each new check is compared with what the estimate was at that moment; the
difference ("drift") is money that moved without being logged: fees,
interest, forgotten entries.
"""
from datetime import date, datetime, timedelta

from .util import TS_FORMAT, now, ts


def latest_check(db, at=None):
    return db.execute(
        "SELECT * FROM balance_checks WHERE checked_at <= ? ORDER BY checked_at DESC, id DESC LIMIT 1",
        (at or "9999",),
    ).fetchone()


def flows(db, after, until):
    """Income and spending logged after `after` up to and including `until`."""
    row = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN is_income THEN amount_cents END), 0) AS income,
                  COALESCE(SUM(CASE WHEN NOT is_income THEN amount_cents END), 0) AS expense,
                  COUNT(*) AS n
           FROM transactions WHERE NOT off_balance AND occurred_at > ? AND occurred_at <= ?""",
        (after, until),
    ).fetchone()
    return dict(row)


def estimate(db, at=None):
    """The estimate at `at` (default now), or None before the first check."""
    at = at or ts(now())
    check = latest_check(db, at)
    if check is None:
        return None
    moved = flows(db, check["checked_at"], at)
    return {
        "check": dict(check),
        "income": moved["income"],
        "expense": moved["expense"],
        "entries": moved["n"],
        "amount": check["amount_cents"] + moved["income"] - moved["expense"],
    }


def record_check(db, amount_cents, source, user_name, note="", at=None):
    """Store a balance check; returns (estimate just before it or None, drift or None)."""
    at = at or ts(now())
    before = estimate(db, at)
    db.execute(
        """INSERT INTO balance_checks (checked_at, amount_cents, note, source, user_name, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (at, amount_cents, note, source, user_name, ts(now())),
    )
    db.commit()
    return before, (amount_cents - before["amount"]) if before else None


def checks(db, limit=12):
    """Recent checks, newest first, each with the drift found at it."""
    rows = db.execute("SELECT * FROM balance_checks ORDER BY checked_at, id").fetchall()
    result = []
    previous = None
    for row in rows:
        entry = dict(row)
        if previous is not None:
            moved = flows(db, previous["checked_at"], row["checked_at"])
            expected = previous["amount_cents"] + moved["income"] - moved["expense"]
            entry["expected"] = expected
            entry["drift"] = row["amount_cents"] - expected
            entry["days"] = (datetime.strptime(row["checked_at"], TS_FORMAT)
                             - datetime.strptime(previous["checked_at"], TS_FORMAT)).days
        result.append(entry)
        previous = row
    return list(reversed(result))[:limit]


def daily(db, until_day, days=365):
    """(dates, estimated end-of-day balances, actual check amounts or None per day)
    from the first check (at most `days` back) to `until_day`."""
    first = db.execute("SELECT MIN(checked_at) FROM balance_checks").fetchone()[0]
    if not first:
        return [], [], []
    start = max(date.fromisoformat(first[:10]), until_day - timedelta(days=days - 1))
    events = [(row["checked_at"], "check", row["amount_cents"])
              for row in db.execute("SELECT checked_at, amount_cents FROM balance_checks ORDER BY checked_at, id")]
    events += [(row["occurred_at"], "flow", row["amount_cents"] if row["is_income"] else -row["amount_cents"])
               for row in db.execute(
                   "SELECT occurred_at, amount_cents, is_income FROM transactions "
                   "WHERE NOT off_balance AND occurred_at > ?", (first,))]
    # Same second: the flow first, as estimate() counts it as already in the check.
    events.sort(key=lambda event: (event[0], event[1] == "check"))

    labels, values, marks = [], [], []
    balance, i = None, 0
    day = date.fromisoformat(first[:10])
    while day <= until_day:
        end_of_day = f"{day.isoformat()} 23:59:59"
        checked = None
        while i < len(events) and events[i][0] <= end_of_day:
            when, kind, amount = events[i]
            if kind == "check":
                balance = amount
                checked = amount
            elif balance is not None:
                balance += amount
            i += 1
        if day >= start:
            labels.append(day.isoformat())
            values.append(balance)
            marks.append(checked)
        day += timedelta(days=1)
    return labels, values, marks
