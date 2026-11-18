"""Read-only JSON for companion apps on the same server (the renovation planner).

    GET /api/v1/liquidity?until=YYYY-MM-DD&category=hisa

Authenticated with `Authorization: Bearer <FINANCE_API_TOKEN>`; without a token
configured the API doesn't exist (404). It's meant to be called over the
internal Docker network: docker-compose.yml keeps /api off the public router.

Returns today's bank & cash estimate, the security fund, the forecast from
forecast.build() (which leaves out Project and Savings one-offs, so a caller
can add its own planned project costs without counting anything twice) and,
with `category`, what was spent on that category per month.
"""
import hmac
from datetime import date, timedelta

from flask import Blueprint, abort, current_app, jsonify, request

from . import categories, forecast, liquidity
from .db import get_db
from .util import now

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
