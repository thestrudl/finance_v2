"""Queries behind the Holdings page."""
from datetime import date, timedelta

CLASSES = [  # snapshot column, label
    ("stock_cents", "Stocks & ETFs"),
    ("funds_cents", "Funds"),
    ("bonds_cents", "Bonds"),
    ("options_cents", "Options"),
    ("cash_cents", "Cash"),
    ("other_cents", "Other (accruals)"),
]


def short_date(day):
    """'2026-10-21' -> '21 Oct 26'."""
    d = date.fromisoformat(day)
    return f"{d.day} {d:%b} {d:%y}"


def latest(db):
    """The newest snapshot of every account, with its positions and cash."""
    rows = db.execute(
        """SELECT s.* FROM holding_snapshots s
           JOIN (SELECT source, account, MAX(as_of) AS as_of FROM holding_snapshots GROUP BY source, account) m
             ON m.source = s.source AND m.account = s.account AND m.as_of = s.as_of
           ORDER BY s.total_cents DESC"""
    ).fetchall()
    accounts = []
    for snap in rows:
        positions = db.execute(
            "SELECT * FROM holding_positions WHERE snapshot_id = ? ORDER BY value_base_cents DESC", (snap["id"],)
        ).fetchall()
        cash = db.execute(
            "SELECT * FROM holding_cash WHERE snapshot_id = ? ORDER BY COALESCE(base_cents, cash_cents) DESC",
            (snap["id"],),
        ).fetchall()
        accounts.append({**dict(snap), "positions": [dict(p) for p in positions], "cash": [dict(c) for c in cash]})
    return accounts


def on_or_before(db, source, account, day):
    """The account's snapshot on `day`, or the closest one before it."""
    return db.execute(
        """SELECT * FROM holding_snapshots WHERE source = ? AND account = ? AND as_of <= ?
           ORDER BY as_of DESC LIMIT 1""",
        (source, account, day),
    ).fetchone()


def history(db, source, account, since):
    return db.execute(
        """SELECT as_of, total_cents, cash_cents FROM holding_snapshots
           WHERE source = ? AND account = ? AND as_of >= ? ORDER BY as_of""",
        (source, account, since),
    ).fetchall()


def account_view(db, snap):
    """Everything the page shows for one account."""
    as_of = date.fromisoformat(snap["as_of"])
    month_ago = on_or_before(db, snap["source"], snap["account"], (as_of - timedelta(days=30)).isoformat())
    year_start = on_or_before(db, snap["source"], snap["account"], f"{as_of.year - 1}-12-31")
    securities = sum(snap[col] for col in ("stock_cents", "funds_cents", "bonds_cents", "options_cents"))
    cost = sum(p["cost_base_cents"] or 0 for p in snap["positions"] if p["cost_base_cents"] is not None)
    unrealized = sum(p["unrealized_base_cents"] or 0 for p in snap["positions"])
    total = snap["total_cents"] or 1
    allocation = [
        {"label": label, "cents": snap[col], "share": snap[col] / total}
        for col, label in CLASSES if snap[col]
    ]
    rows = history(db, snap["source"], snap["account"], (as_of - timedelta(days=365)).isoformat())
    chart = {
        "type": "line",
        "labels": [short_date(r["as_of"]) for r in rows],
        "series": [
            {"name": "Account value", "values": [r["total_cents"] for r in rows], "color": "--ink"},
            {"name": "Cash", "values": [r["cash_cents"] for r in rows], "color": "--s1"},
        ],
    }
    return {
        **snap,
        "securities": securities,
        "cost": cost,
        "unrealized": unrealized,
        "month_ago": dict(month_ago) if month_ago and month_ago["as_of"] != snap["as_of"] else None,
        "year_start": dict(year_start) if year_start else None,
        "allocation": allocation,
        "chart": chart,
        "history_days": len(rows),
    }
