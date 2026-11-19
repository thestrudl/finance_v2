"""JSON for companion apps on the same server (the renovation planner).

    GET    /api/v1/liquidity?until=YYYY-MM-DD&category=hisa
    GET    /api/v1/transactions?category=hisa   expenses in a category, to link to the planner
    PUT    /api/v1/transactions/<key>   log or change an expense (a renovation payment)
    DELETE /api/v1/transactions/<key>   remove it again

Authenticated with `Authorization: Bearer <FINANCE_API_TOKEN>`; without a token
configured the API doesn't exist (404). It's meant to be called over the
internal Docker network: docker-compose.yml keeps /api off the public router.

Returns today's bank & cash estimate, the security fund, the forecast from
forecast.build() (which leaves out Project and Savings one-offs, so a caller
can add its own planned project costs without counting anything twice) and,
with `category`, what was spent on that category per month.

Expenses logged through the API are ordinary transactions (source 'web'),
remembered in api_entries under the caller's key: sending the same key again
changes that transaction instead of logging a second one.
"""
import hmac
import re
from datetime import date, timedelta

from flask import Blueprint, abort, current_app, jsonify, request

from . import categories, forecast, liquidity
from .db import get_db
from .util import now, ts

KEY = re.compile(r"[A-Za-z0-9_.-]{1,100}")
MAX_CENTS = 10**11

bp = Blueprint("api", __name__, url_prefix="/api/v1")


@bp.before_request
def require_token():
    expected = current_app.config.get("FINANCE_API_TOKEN", "")
    if not expected:
        abort(404)
    sent = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(sent.encode(), expected.encode()):
        abort(401)


def _until(today):
    try:
        chosen = date.fromisoformat(request.args.get("until", ""))
    except ValueError:
        chosen = today + timedelta(days=365)
    return min(max(chosen, today + timedelta(days=1)), today + timedelta(days=forecast.MAX_DAYS))


def _category_spend(db, raw):
    """Net spending (refunds subtract) on one category, by month, oldest first."""
    key = categories.lookup(db, raw)
    found = db.execute("SELECT label FROM categories WHERE key = ?", (key,)).fetchone()
    if found is None:
        return {"category": raw.strip().lower(), "found": False, "months": [], "total_cents": 0}
    rows = db.execute(
        """SELECT substr(occurred_at, 1, 7) AS month, SUM(amount_cents) AS cents FROM transactions
           WHERE category = ? AND NOT is_income GROUP BY month ORDER BY month""",
        (key,),
    ).fetchall()
    label = found["label"]
    months = [{"month": r["month"], "cents": r["cents"]} for r in rows]
    return {"category": key, "label": label, "found": True, "months": months,
            "total_cents": sum(m["cents"] for m in months)}


@bp.get("/liquidity")
def liquidity_view():
    db = get_db()
    today = now().date()
    until = _until(today)
    current = liquidity.estimate(db)
    fund = liquidity.security_fund(db)
    start = current["amount"] if current else 0
    result = forecast.build(db, today, until, start, (), fund)
    body = {
        "today": today.isoformat(),
        "balance": None if current is None else {
            "amount_cents": current["amount"],
            "checked_at": current["check"]["checked_at"],
            "entries_since": current["entries"],
        },
        "security_fund_cents": fund,
        "forecast": {
            "until": until.isoformat(),
            "days": [day.isoformat() for day in result["days"]],
            "balances": result["balances"],
            "history_months": result["history_months"],
        },
    }
    if request.args.get("category", "").strip():
        body["category_spend"] = _category_spend(db, request.args["category"])
    return jsonify(body)


@bp.get("/transactions")
def list_transactions():
    """Expenses in one category, newest first, each with the API key it was
    logged under (None when it came from anywhere else)."""
    db = get_db()
    key = categories.lookup(db, request.args.get("category", ""))
    if not key or db.execute("SELECT 1 FROM categories WHERE key = ?", (key,)).fetchone() is None:
        return _bad(f"There's no category {request.args.get('category', '')!r} in finance.")
    rows = db.execute(
        """SELECT t.id, t.occurred_at, t.amount_cents, t.comment, t.user_name, t.off_balance, e.key AS api_key
           FROM transactions t LEFT JOIN api_entries e ON e.transaction_id = t.id
           WHERE t.category = ? AND NOT t.is_income ORDER BY t.occurred_at DESC, t.id DESC LIMIT 2000""",
        (key,),
    ).fetchall()
    return jsonify({"category": key, "transactions": [
        {**dict(r), "off_balance": bool(r["off_balance"])} for r in rows]})


def _bad(message):
    return jsonify({"error": message}), 400


@bp.put("/transactions/<key>")
def put_transaction(key):
    """{"day": "YYYY-MM-DD", "amount_cents": int, "category": "hisa", "comment": str,
    "user_name": str, "off_balance": bool} -> {"transaction_id", "created"}."""
    if not KEY.fullmatch(key):
        return _bad("The key may hold letters, digits, '.', '_' and '-', up to 100.")
    body = request.get_json(silent=True) or {}
    try:
        day = date.fromisoformat(str(body.get("day", "")))
    except ValueError:
        return _bad("day must be YYYY-MM-DD.")
    cents = body.get("amount_cents")
    if not isinstance(cents, int) or isinstance(cents, bool) or cents == 0 or abs(cents) > MAX_CENTS:
        return _bad("amount_cents must be a whole number of cents, not 0.")
    db = get_db()
    category = categories.lookup(db, str(body.get("category", "")))
    if not category or db.execute("SELECT 1 FROM categories WHERE key = ?", (category,)).fetchone() is None:
        return _bad(f"There's no category {body.get('category')!r} in finance.")
    comment = str(body.get("comment", ""))[:500]
    user_name = str(body.get("user_name", ""))[:100] or None
    off_balance = int(bool(body.get("off_balance")))
    moment = now()
    # Today's payments get the time they were entered, others midday.
    occurred_at = ts(moment) if day == moment.date() else f"{day.isoformat()} 12:00:00"

    db.execute("BEGIN IMMEDIATE")  # two retries of the same key must not both insert
    try:
        entry = db.execute(
            """SELECT e.transaction_id FROM api_entries e JOIN transactions t ON t.id = e.transaction_id
               WHERE e.key = ?""", (key,)).fetchone()
        if entry:
            db.execute(
                """UPDATE transactions SET occurred_at = ?, amount_cents = ?, category = ?, category_raw = ?,
                       comment = ?, user_name = ?, off_balance = ? WHERE id = ?""",
                (occurred_at, cents, category, category, comment, user_name, off_balance, entry["transaction_id"]),
            )
            tx_id, created = entry["transaction_id"], False
        else:
            tx_id = db.execute(
                """INSERT INTO transactions (occurred_at, user_name, amount_cents, category, category_raw, comment,
                       is_income, source, off_balance, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, 'web', ?, ?)""",
                (occurred_at, user_name, cents, category, category, comment, off_balance, ts(moment)),
            ).lastrowid
            db.execute(
                """INSERT INTO api_entries (key, transaction_id, created_at) VALUES (?, ?, ?)
                   ON CONFLICT (key) DO UPDATE SET transaction_id = excluded.transaction_id""",
                (key, tx_id, ts(moment)),
            )
            created = True
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"transaction_id": tx_id, "created": created}), 201 if created else 200


@bp.delete("/transactions/<key>")
def delete_transaction(key):
    """Remove what was logged under `key`; {"deleted": false} if there's nothing (any more)."""
    db = get_db()
    entry = db.execute("SELECT transaction_id FROM api_entries WHERE key = ?", (key,)).fetchone()
    deleted = False
    if entry and entry["transaction_id"]:
        deleted = db.execute("DELETE FROM transactions WHERE id = ?", (entry["transaction_id"],)).rowcount == 1
    db.execute("DELETE FROM api_entries WHERE key = ?", (key,))
    db.commit()
    return jsonify({"deleted": deleted})
