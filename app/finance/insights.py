"""Queries behind the dashboard and the Telegram reports.

Scope "core" leaves out the project and savings groups (house, wedding,
savings transfers) so one-off amounts don't swamp everyday spending.
"""
import calendar
import itertools
import statistics
from datetime import date, datetime, timedelta

from .categories import GROUPS
from .util import TS_FORMAT, month_bounds, shift_month

SCOPES = {"all": (), "core": ("project", "savings")}


def _scope(scope):
    groups = SCOPES.get(scope, ())
    if not groups:
        return "", []
    return f" AND c.grp NOT IN ({', '.join('?' * len(groups))})", list(groups)


SUMS = """
    COALESCE(SUM(CASE WHEN t.is_income THEN t.amount_cents END), 0) AS income,
    COALESCE(SUM(CASE WHEN NOT t.is_income THEN t.amount_cents END), 0) AS expense,
    COALESCE(SUM(CASE WHEN NOT t.is_income AND c.grp = 'living' THEN t.amount_cents END), 0) AS living
"""


def totals(db, start, end, scope="all"):
    where, params = _scope(scope)
    row = db.execute(
        f"""SELECT {SUMS}, COUNT(*) AS n
            FROM transactions t JOIN categories c ON c.key = t.category
            WHERE t.occurred_at >= ? AND t.occurred_at < ? {where}""",
        [start, end, *params],
    ).fetchone()
    result = dict(row)
    result["net"] = result["income"] - result["expense"]
    result["savings_rate"] = result["net"] / result["income"] if result["income"] > 0 else None
    return result


def monthly_series(db, last_ym, months=12, scope="all"):
    first_ym = shift_month(last_ym, -(months - 1))
    where, params = _scope(scope)
    rows = db.execute(
        f"""SELECT substr(t.occurred_at, 1, 7) AS month, {SUMS}
            FROM transactions t JOIN categories c ON c.key = t.category
            WHERE t.occurred_at >= ? AND t.occurred_at < ? {where}
            GROUP BY month""",
        [month_bounds(first_ym)[0], month_bounds(last_ym)[1], *params],
    ).fetchall()
    by_month = {row["month"]: dict(row) for row in rows}
    series = []
    for i in range(months):
        ym = shift_month(first_ym, i)
        point = by_month.get(ym, {"month": ym, "income": 0, "expense": 0, "living": 0})
        point["net"] = point["income"] - point["expense"]
        series.append(point)
    return series


def months_with_data(db, start, end):
    row = db.execute(
        "SELECT COUNT(DISTINCT substr(occurred_at, 1, 7)) FROM transactions WHERE occurred_at >= ? AND occurred_at < ?",
        (start, end),
    ).fetchone()
    return row[0]


def category_breakdown(db, ym, is_income, scope="all"):
    """Per-category totals for a month next to the average of the 12 months before it."""
    start, end = month_bounds(ym)
    prev_start = month_bounds(shift_month(ym, -12))[0]
    history_months = max(months_with_data(db, prev_start, start), 1)
    where, params = _scope(scope)
    rows = db.execute(
        f"""WITH cur AS (
                SELECT category, SUM(amount_cents) AS total, COUNT(*) AS n FROM transactions
                WHERE is_income = ? AND occurred_at >= ? AND occurred_at < ? GROUP BY category),
            prev AS (
                SELECT category, SUM(amount_cents) AS total FROM transactions
                WHERE is_income = ? AND occurred_at >= ? AND occurred_at < ? GROUP BY category)
            SELECT c.key, c.label, c.grp, c.budget_cents, c.budget_period,
                   cur.total AS total, cur.n AS n, COALESCE(prev.total, 0) AS prev_total
            FROM categories c JOIN cur ON cur.category = c.key
            LEFT JOIN prev ON prev.category = c.key
            WHERE cur.total != 0 {where}
            ORDER BY cur.total DESC""",
        [int(is_income), start, end, int(is_income), prev_start, start, *params],
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["avg"] = round(item.pop("prev_total") / history_months)
        result.append(item)
    return result


def budgets(db, ym, today):
    """Monthly budgets against the month; yearly budgets against year-to-date,
    with the share of the year that has passed as the pace to beat."""
    year = int(ym[:4])
    month_start, month_end = month_bounds(ym)
    year_start = f"{year}-01-01 00:00:00"
    is_current = ym == f"{today.year:04d}-{today.month:02d}"
    days_in_month = calendar.monthrange(year, int(ym[5:]))[1]
    month_elapsed = today.day / days_in_month if is_current else 1.0
    period_end_day = today if is_current else date(year, int(ym[5:]), days_in_month)
    year_days = 366 if calendar.isleap(year) else 365
    year_elapsed = period_end_day.timetuple().tm_yday / year_days

    result = []
    for cat in db.execute(
        "SELECT key, label, budget_cents, budget_period FROM categories "
        "WHERE budget_cents > 0 AND budget_period IS NOT NULL ORDER BY budget_period, label"
    ):
        start = month_start if cat["budget_period"] == "month" else year_start
        spent = db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions "
            "WHERE NOT is_income AND category = ? AND occurred_at >= ? AND occurred_at < ?",
            (cat["key"], start, month_end),
        ).fetchone()[0]
        elapsed = month_elapsed if cat["budget_period"] == "month" else year_elapsed
        limit = cat["budget_cents"]
        if spent > limit:
            status = "over"
        elif spent > limit * elapsed:
            status = "ahead"
        else:
            status = "ok"
        result.append({
            **dict(cat),
            "spent": spent,
            "share": spent / limit,
            "pace": elapsed,
            "status": status,
        })
    return result


def year_matrix(db, year, is_income, scope="all"):
    where, params = _scope(scope)
    rows = db.execute(
        f"""SELECT t.category, c.label, c.grp,
                   CAST(substr(t.occurred_at, 6, 2) AS INTEGER) AS month,
                   SUM(t.amount_cents) AS total
            FROM transactions t JOIN categories c ON c.key = t.category
            WHERE t.is_income = ? AND t.occurred_at >= ? AND t.occurred_at < ? {where}
            GROUP BY t.category, month""",
        [int(is_income), f"{year}-01-01", f"{year + 1}-01-01", *params],
    ).fetchall()
    matrix = {}
    for row in rows:
        entry = matrix.setdefault(row["category"], {
            "key": row["category"], "label": row["label"], "grp": row["grp"], "months": [0] * 12,
        })
        entry["months"][row["month"] - 1] = row["total"]
    result = []
    for entry in matrix.values():
        entry["total"] = sum(entry["months"])
        peak = max(entry["months"])
        # Shade each cell against its own row: shows when a category peaks.
        entry["heat"] = [
            0 if value <= 0 or peak <= 0 else min(5, 1 + int(4.999 * value / peak))
            for value in entry["months"]
        ]
        result.append(entry)
    result.sort(key=lambda entry: -entry["total"])
    column_totals = [sum(entry["months"][i] for entry in result) for i in range(12)]
    return result, column_totals


def business_pnl(db, year, ym):
    month_start, month_end = month_bounds(ym)
    rows = db.execute(
        """SELECT c.key, c.label,
               COALESCE(SUM(CASE WHEN t.is_income THEN t.amount_cents END), 0) AS income,
               COALESCE(SUM(CASE WHEN NOT t.is_income THEN t.amount_cents END), 0) AS expense,
               COALESCE(SUM(CASE WHEN t.occurred_at >= ? AND t.occurred_at < ?
                   THEN CASE WHEN t.is_income THEN t.amount_cents ELSE -t.amount_cents END END), 0) AS month_net
           FROM transactions t JOIN categories c ON c.key = t.category
           WHERE c.grp = 'business' AND t.occurred_at >= ? AND t.occurred_at < ?
           GROUP BY c.key ORDER BY income DESC""",
        (month_start, month_end, f"{year}-01-01", f"{year + 1}-01-01"),
    ).fetchall()
    return [{**dict(row), "net": row["income"] - row["expense"]} for row in rows]


def largest_expenses(db, start, end, scope="all", limit=8):
    where, params = _scope(scope)
    return db.execute(
        f"""SELECT t.*, c.label FROM transactions t JOIN categories c ON c.key = t.category
            WHERE NOT t.is_income AND t.occurred_at >= ? AND t.occurred_at < ? {where}
            ORDER BY t.amount_cents DESC LIMIT ?""",
        [start, end, *params, limit],
    ).fetchall()


def unusual_expenses(db, start, end, scope="all", limit=8):
    """Expenses at least 3x the category's median over the year before."""
    where, params = _scope(scope)
    candidates = db.execute(
        f"""SELECT t.*, c.label FROM transactions t JOIN categories c ON c.key = t.category
            WHERE NOT t.is_income AND t.amount_cents >= 3000
              AND t.occurred_at >= ? AND t.occurred_at < ? {where}""",
        [start, end, *params],
    ).fetchall()
    history_start = (datetime.strptime(start, TS_FORMAT) - timedelta(days=365)).strftime(TS_FORMAT)
    medians = {}
    flagged = []
    for row in candidates:
        key = row["category"]
        if key not in medians:
            amounts = [r[0] for r in db.execute(
                "SELECT amount_cents FROM transactions WHERE NOT is_income AND amount_cents > 0 "
                "AND category = ? AND occurred_at >= ? AND occurred_at < ?",
                (key, history_start, start),
            )]
            medians[key] = statistics.median(amounts) if len(amounts) >= 5 else None
        median = medians[key]
        if median and row["amount_cents"] >= 3 * median:
            flagged.append({**dict(row), "median": median, "ratio": row["amount_cents"] / median})
    flagged.sort(key=lambda item: -item["ratio"])
    return flagged[:limit]


def category_totals(db, start, end):
    """{(category key, is_income): cents} for a time window."""
    rows = db.execute(
        "SELECT category, is_income, SUM(amount_cents) AS total FROM transactions "
        "WHERE occurred_at >= ? AND occurred_at < ? GROUP BY category, is_income",
        (start, end),
    ).fetchall()
    return {(row["category"], bool(row["is_income"])): row["total"] for row in rows}


def data_range(db):
    row = db.execute("SELECT MIN(occurred_at), MAX(occurred_at) FROM transactions").fetchone()
    if not row[0]:
        return None, None
    return row[0][:7], row[1][:7]


def category_series(db, key, last_ym, months=24):
    first_ym = shift_month(last_ym, -(months - 1))
    rows = db.execute(
        """SELECT substr(occurred_at, 1, 7) AS month,
               COALESCE(SUM(CASE WHEN NOT is_income THEN amount_cents END), 0) AS expense,
               COALESCE(SUM(CASE WHEN is_income THEN amount_cents END), 0) AS income
           FROM transactions WHERE category = ? AND occurred_at >= ? AND occurred_at < ?
           GROUP BY month""",
        (key, month_bounds(first_ym)[0], month_bounds(last_ym)[1]),
    ).fetchall()
    by_month = {row["month"]: dict(row) for row in rows}
    return [
        by_month.get(shift_month(first_ym, i), {"month": shift_month(first_ym, i), "expense": 0, "income": 0})
        for i in range(months)
    ]


# ── monthly dashboard ───────────────────────────────────────────────────────
# Living costs swing from refunds to a car purchase in one month, so "typical"
# means the median of recent months, not the mean.

def months_present(db, first_ym, last_ym):
    rows = db.execute(
        "SELECT DISTINCT substr(occurred_at, 1, 7) FROM transactions WHERE occurred_at >= ? AND occurred_at < ?",
        (month_bounds(first_ym)[0], month_bounds(last_ym)[1]),
    ).fetchall()
    return {row[0] for row in rows}


def typical_month(db, ym, scope="all", months=12):
    """Median income / expense / living / net over the months with data before `ym`."""
    have = months_present(db, shift_month(ym, -months), shift_month(ym, -1))
    series = [p for p in monthly_series(db, shift_month(ym, -1), months, scope) if p["month"] in have]
    if not series:
        return None
    return {key: round(statistics.median(p[key] for p in series)) for key in ("income", "expense", "living", "net")}


def daily_living(db, ym):
    """Cumulative living costs by day of the month."""
    start, end = month_bounds(ym)
    per_day = [0] * calendar.monthrange(int(ym[:4]), int(ym[5:]))[1]
    for day, total in db.execute(
        """SELECT CAST(substr(t.occurred_at, 9, 2) AS INTEGER), SUM(t.amount_cents)
           FROM transactions t JOIN categories c ON c.key = t.category
           WHERE NOT t.is_income AND c.grp = 'living' AND t.occurred_at >= ? AND t.occurred_at < ?
           GROUP BY 1""",
        (start, end),
    ):
        per_day[day - 1] = total
    return list(itertools.accumulate(per_day))


def living_pace(db, ym, day=None, months=12):
    """This month's cumulative living costs against the typical month.

    Returns the median cumulative curve of earlier months (31 points) and, when
    `day` is given (month in progress), a projected month total: spent so far
    plus the median of what earlier months still spent after that day."""
    curves = []
    have = months_present(db, shift_month(ym, -months), shift_month(ym, -1))
    for i in range(1, months + 1):
        earlier = shift_month(ym, -i)
        if earlier in have:
            curve = daily_living(db, earlier)
            curves.append(curve + [curve[-1]] * (31 - len(curve)))
    current = daily_living(db, ym)
    result = {"current": current, "typical": None, "typical_total": None, "projected": None}
    if curves:
        result["typical"] = [round(statistics.median(c[d] for c in curves)) for d in range(31)]
        result["typical_total"] = round(statistics.median(c[-1] for c in curves))
        if day:
            remaining = statistics.median(c[-1] - c[day - 1] for c in curves)
            result["projected"] = current[day - 1] + round(remaining)
    return result


def recurring(db):
    """Monthly repeats: what is committed every month, whatever else happens."""
    row = db.execute(
        """SELECT COUNT(*) AS n,
               COALESCE(SUM(CASE WHEN NOT is_income THEN amount_cents END), 0) AS expense,
               COALESCE(SUM(CASE WHEN is_income THEN amount_cents END), 0) AS income
           FROM repeats"""
    ).fetchone()
    return dict(row)


# ── yearly dashboard ────────────────────────────────────────────────────────

def year_windows(year, today):
    """(start, end, previous start, previous end). A year in progress is
    compared with the same stretch of the year before, up to the same date."""
    start, prev_start = f"{year}-01-01 00:00:00", f"{year - 1}-01-01 00:00:00"
    if year != today.year:
        return start, f"{year + 1}-01-01 00:00:00", prev_start, start
    end_day = today + timedelta(days=1)
    try:
        prev_end_day = end_day.replace(year=year - 1)
    except ValueError:  # 29 February
        prev_end_day = date(year - 1, 3, 1)
    return start, f"{end_day.isoformat()} 00:00:00", prev_start, f"{prev_end_day.isoformat()} 00:00:00"


def has_data(db, start, end):
    return db.execute(
        "SELECT 1 FROM transactions WHERE occurred_at >= ? AND occurred_at < ? LIMIT 1", (start, end)
    ).fetchone() is not None


def group_by_month(db, year, is_income, scope="all"):
    """{group: [12 monthly totals]} for the groups that have entries, in GROUPS order."""
    where, params = _scope(scope)
    totals = {}
    for grp, month, total in db.execute(
        f"""SELECT c.grp, CAST(substr(t.occurred_at, 6, 2) AS INTEGER), SUM(t.amount_cents)
            FROM transactions t JOIN categories c ON c.key = t.category
            WHERE t.is_income = ? AND t.occurred_at >= ? AND t.occurred_at < ? {where}
            GROUP BY 1, 2""",
        [int(is_income), f"{year}-01-01", f"{year + 1}-01-01", *params],
    ):
        totals.setdefault(grp, [0] * 12)[month - 1] = total
    return {grp: totals[grp] for grp in GROUPS if grp in totals}


def cumulative_net(db, year, last_month=12, scope="all"):
    """Running total of income minus spending through the year; None after `last_month`."""
    series = monthly_series(db, f"{year}-12", 12, scope)
    running = list(itertools.accumulate(p["net"] for p in series))
    return [value if i < last_month else None for i, value in enumerate(running)]


def category_changes(db, start, end, prev_start, prev_end, is_income, scope="all"):
    """Per category: this period, the same period a year earlier, and the change."""
    where, params = _scope(scope)
    rows = db.execute(
        f"""SELECT c.key, c.label, c.grp,
               COALESCE(SUM(CASE WHEN t.occurred_at >= ? AND t.occurred_at < ? THEN t.amount_cents END), 0) AS total,
               COALESCE(SUM(CASE WHEN t.occurred_at >= ? AND t.occurred_at < ? THEN t.amount_cents END), 0) AS prev
            FROM transactions t JOIN categories c ON c.key = t.category
            WHERE t.is_income = ? AND t.occurred_at >= ? AND t.occurred_at < ? {where}
            GROUP BY c.key HAVING total != 0 OR prev != 0
            ORDER BY total DESC""",
        [start, end, prev_start, prev_end, int(is_income), prev_start, end, *params],
    ).fetchall()
    grand = sum(row["total"] for row in rows) or 1
    return [{**dict(row), "change": row["total"] - row["prev"], "share": row["total"] / grand} for row in rows]


def business_by_month(db, year):
    """Each Business-group category: net per month this year, totals, and last year's net."""
    rows = db.execute(
        """SELECT c.key, c.label, CAST(substr(t.occurred_at, 1, 4) AS INTEGER) AS y,
               CAST(substr(t.occurred_at, 6, 2) AS INTEGER) AS m,
               SUM(CASE WHEN t.is_income THEN t.amount_cents ELSE 0 END) AS income,
               SUM(CASE WHEN t.is_income THEN 0 ELSE t.amount_cents END) AS expense
           FROM transactions t JOIN categories c ON c.key = t.category
           WHERE c.grp = 'business' AND t.occurred_at >= ? AND t.occurred_at < ?
           GROUP BY c.key, y, m""",
        (f"{year - 1}-01-01", f"{year + 1}-01-01"),
    ).fetchall()
    result = {}
    for row in rows:
        entry = result.setdefault(row["key"], {
            "key": row["key"], "label": row["label"], "net": [0] * 12,
            "income": 0, "expense": 0, "prev_net": 0,
        })
        if row["y"] == year:
            entry["net"][row["m"] - 1] = row["income"] - row["expense"]
            entry["income"] += row["income"]
            entry["expense"] += row["expense"]
        else:
            entry["prev_net"] += row["income"] - row["expense"]
    for entry in result.values():
        entry["total"] = entry["income"] - entry["expense"]
    return sorted(result.values(), key=lambda entry: -entry["income"])


# ── living costs page ───────────────────────────────────────────────────────

def living_categories(db, last_ym, months=12):
    """Living-group spending per category, month by month, for the `months`
    months ending with `last_ym`; biggest total first."""
    first = shift_month(last_ym, -(months - 1))
    index = {shift_month(first, i): i for i in range(months)}
    result = {}
    for row in db.execute(
        """SELECT c.key, c.label, c.budget_cents, c.budget_period, substr(t.occurred_at, 1, 7) AS month,
                  SUM(t.amount_cents) AS total
           FROM transactions t JOIN categories c ON c.key = t.category
           WHERE NOT t.is_income AND c.grp = 'living' AND t.occurred_at >= ? AND t.occurred_at < ?
           GROUP BY c.key, month""",
        (month_bounds(first)[0], month_bounds(last_ym)[1]),
    ):
        entry = result.setdefault(row["key"], {
            "key": row["key"], "label": row["label"], "budget_cents": row["budget_cents"],
            "budget_period": row["budget_period"], "months": [0] * months,
        })
        entry["months"][index[row["month"]]] = row["total"]
    for entry in result.values():
        entry["total"] = sum(entry["months"])
        entry["median"] = round(statistics.median(entry["months"]))
    return sorted(result.values(), key=lambda entry: -entry["total"])


# ── business pages ──────────────────────────────────────────────────────────

def months_between(first_ym, last_ym):
    (y1, m1), (y2, m2) = ((int(p) for p in ym.split("-")) for ym in (first_ym, last_ym))
    return (y2 * 12 + m2) - (y1 * 12 + m1) + 1


def business_totals(db, key, start, end):
    row = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN is_income THEN amount_cents END), 0) AS income,
                  COALESCE(SUM(CASE WHEN NOT is_income THEN amount_cents END), 0) AS expense,
                  COUNT(*) AS n
           FROM transactions WHERE category = ? AND occurred_at >= ? AND occurred_at < ?""",
        (key, start, end),
    ).fetchone()
    result = dict(row)
    result["net"] = result["income"] - result["expense"]
    result["margin"] = result["net"] / result["income"] if result["income"] > 0 else None
    return result


def business_years(db, key):
    rows = db.execute(
        """SELECT substr(occurred_at, 1, 4) AS year,
                  COALESCE(SUM(CASE WHEN is_income THEN amount_cents END), 0) AS income,
                  COALESCE(SUM(CASE WHEN NOT is_income THEN amount_cents END), 0) AS expense
           FROM transactions WHERE category = ? GROUP BY year ORDER BY year""",
        (key,),
    ).fetchall()
    result = []
    for row in rows:
        net = row["income"] - row["expense"]
        result.append({**dict(row), "net": net, "margin": net / row["income"] if row["income"] > 0 else None})
    return result


def profit_since_start(db, key, last_ym):
    """Month labels and the running profit from the business's first entry to `last_ym`."""
    first = db.execute("SELECT MIN(occurred_at) FROM transactions WHERE category = ?", (key,)).fetchone()[0]
    if not first:
        return [], []
    series = category_series(db, key, last_ym, months_between(first[:7], last_ym))
    running = list(itertools.accumulate(p["income"] - p["expense"] for p in series))
    return [p["month"] for p in series], running


def largest_entries(db, key, start, end, is_income, limit=6):
    return db.execute(
        """SELECT * FROM transactions WHERE category = ? AND is_income = ? AND occurred_at >= ? AND occurred_at < ?
           ORDER BY amount_cents DESC LIMIT ?""",
        (key, int(is_income), start, end, limit),
    ).fetchall()
