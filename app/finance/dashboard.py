"""Insights portal. Every view needs a login."""
import calendar
import itertools
import statistics
from datetime import timedelta
from decimal import InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from . import categories, holdings, ibkr, importer, insights, liquidity
from .bot import run_in_background
from .auth import login_required
from .categories import GROUPS
from .db import get_db
from .util import fmt_eur, fmt_month, month_bounds, month_key, now, shift_month, to_cents, ts, valid_month

bp = Blueprint("dashboard", __name__)

PAGE_SIZE = 100


@bp.before_request
@login_required
def require_login():
    return None


def _month_arg():
    ym = request.args.get("month", "")
    return ym if valid_month(ym) else month_key(now())


def _back(target, fallback):
    """Only follow redirects to paths on this site."""
    return target if target and target.startswith("/") and not target.startswith("//") else fallback


def _scope_arg():
    scope = request.args.get("scope", "all")
    return scope if scope in insights.SCOPES else "all"


@bp.get("/")
def overview():
    db = get_db()
    today = now()
    ym = _month_arg()
    scope = _scope_arg()
    start, end = month_bounds(ym)

    current = insights.totals(db, start, end, scope)
    typical = insights.typical_month(db, ym, scope) or {}
    last_year_ym = shift_month(ym, -12)
    last_year_bounds = month_bounds(last_year_ym)
    last_year = insights.totals(db, *last_year_bounds, scope) if insights.has_data(db, *last_year_bounds) else None

    is_current = ym == month_key(today)
    days_in_month = calendar.monthrange(int(ym[:4]), int(ym[5:]))[1]
    first_ym, last_ym = insights.data_range(db)

    expenses = insights.category_breakdown(db, ym, False, scope)
    income = insights.category_breakdown(db, ym, True, scope)
    trend = insights.monthly_series(db, ym, 12, scope)

    pace = insights.living_pace(db, ym, today.day if is_current else None)
    shown_days = today.day if is_current else days_in_month
    pace_chart = {
        "type": "line",
        "labels": [str(day) for day in range(1, days_in_month + 1)],
        "series": [{"name": "This month", "values": pace["current"][:shown_days], "color": "--s2"}],
    }
    if pace["typical"]:
        pace_chart["series"].append(
            {"name": "Typical month", "values": pace["typical"][:days_in_month], "color": "--ref"})

    return render_template(
        "overview.html",
        ym=ym,
        scope=scope,
        prev_ym=shift_month(ym, -1),
        next_ym=shift_month(ym, 1) if not is_current else None,
        first_ym=first_ym,
        is_current=is_current,
        day_of_month=today.day,
        days_in_month=days_in_month,
        current=current,
        typical=typical,
        last_year=last_year,
        last_year_ym=last_year_ym,
        pace=pace,
        pace_chart=pace_chart,
        recurring=insights.recurring(db),
        expenses=expenses,
        expense_max=max((abs(row["total"]) for row in expenses), default=1) or 1,
        income=income,
        income_max=max((abs(row["total"]) for row in income), default=1) or 1,
        budgets=insights.budgets(db, ym, today.date()),
        business=insights.business_pnl(db, int(ym[:4]), ym),
        largest=insights.largest_expenses(db, start, end, scope),
        unusual=insights.unusual_expenses(db, start, end, scope),
        trend=trend,
        last_ym=last_ym,
    )


@bp.get("/year")
def year_view():
    db = get_db()
    today = now()
    try:
        year = int(request.args.get("year", today.year))
    except ValueError:
        year = today.year
    scope = _scope_arg()
    is_current = year == today.year
    months_elapsed = today.month if is_current else 12
    month_names = [calendar.month_abbr[i] for i in range(1, 13)]
    first_ym, _ = insights.data_range(db)

    start, end, prev_start, prev_end = insights.year_windows(year, today.date())
    current = insights.totals(db, start, end, scope)
    previous = insights.totals(db, prev_start, prev_end, scope) if insights.has_data(db, prev_start, prev_end) else None

    monthly = insights.monthly_series(db, f"{year}-12", 12, scope)
    shown = monthly[:months_elapsed]
    living_months = [p["living"] for p in shown]
    typical_living = round(statistics.median(living_months)) if living_months else 0
    previous_year_months = [p["living"] for p in insights.monthly_series(db, f"{year - 1}-12", 12, scope)
                            if p["month"] in insights.months_present(db, f"{year - 1}-01", f"{year - 1}-12")]
    typical_living_prev = round(statistics.median(previous_year_months)) if previous_year_months else None

    def until_now(values):
        return [value if i < months_elapsed else None for i, value in enumerate(values)]

    def group_chart(groups, names):
        return {
            "type": "stacked",
            "labels": month_names,
            "series": [{"name": names[grp], "values": until_now(values), "color": f"--g-{grp}"}
                       for grp, values in groups.items()],
        }

    spending_groups = insights.group_by_month(db, year, False, scope)
    income_groups = insights.group_by_month(db, year, True, scope)
    has_last_year = insights.has_data(db, f"{year - 1}-01-01", f"{year}-01-01")
    charts = {
        "monthly": {
            "type": "bar", "labels": month_names, "net": True,
            "series": [
                {"name": "Income", "values": until_now([p["income"] for p in monthly]), "color": "--s1"},
                {"name": "Spending", "values": until_now([p["expense"] for p in monthly]), "color": "--s2"},
            ],
        },
        "running": {
            "type": "line", "labels": month_names,
            "series": [{"name": str(year), "values": insights.cumulative_net(db, year, months_elapsed, scope),
                        "color": "--ink"}]
            + ([{"name": str(year - 1), "values": insights.cumulative_net(db, year - 1, 12, scope),
                 "color": "--ref"}] if has_last_year else []),
        },
        "spending_groups": group_chart(spending_groups, GROUPS),
        "income_groups": group_chart(income_groups, {**GROUPS, "living": "Salary & other"}),
    }

    expense_changes = insights.category_changes(db, start, end, prev_start, prev_end, False, scope)
    income_changes = insights.category_changes(db, start, end, prev_start, prev_end, True, scope)
    movers = sorted((row for row in expense_changes if abs(row["change"]) >= 10000), key=lambda row: row["change"])
    expenses, expense_totals = insights.year_matrix(db, year, False, scope)
    income, income_totals = insights.year_matrix(db, year, True, scope)
    return render_template(
        "year.html",
        year=year,
        scope=scope,
        is_current=is_current,
        through=f"{today.day} {calendar.month_abbr[today.month]}",
        first_year=int(first_ym[:4]) if first_ym else today.year,
        first_month=first_ym,
        this_year=today.year,
        current=current,
        previous=previous,
        typical_living=typical_living,
        typical_living_prev=typical_living_prev,
        recurring=insights.recurring(db),
        charts=charts,
        monthly=shown,
        spending_groups=spending_groups,
        income_groups=income_groups,
        groups=GROUPS,
        increases=[row for row in reversed(movers) if row["change"] > 0][:5] if previous else [],
        decreases=[row for row in movers if row["change"] < 0][:5] if previous else [],
        expense_changes=expense_changes,
        income_changes=income_changes,
        business=insights.business_by_month(db, year),
        expenses=expenses,
        expense_totals=expense_totals,
        income=income,
        income_totals=income_totals,
        months_elapsed=months_elapsed,
        month_names=month_names,
    )


@bp.get("/living")
def living_view():
    """Everyday costs only: the Living group (food, car, gas, clothing, ...)."""
    db = get_db()
    today = now()
    ym = _month_arg()
    is_current = ym == month_key(today)
    first_ym, _ = insights.data_range(db)

    history = insights.living_categories(db, shift_month(ym, -1), 12)  # the 12 full months before
    window = insights.living_categories(db, ym, 12)                     # charts: 12 months up to this one
    window_by_key = {row["key"]: row for row in window}
    current = {row["key"]: row["months"][-1] for row in window}
    history_bounds = (month_bounds(shift_month(ym, -12))[0], month_bounds(ym)[0])
    history_months = max(insights.months_with_data(db, *history_bounds), 1)
    history_total = sum(row["total"] for row in history)
    before_bounds = (month_bounds(shift_month(ym, -24))[0], history_bounds[0])
    before_total = insights.totals(db, *before_bounds)["living"] if insights.has_data(db, *before_bounds) else None
    typical = (insights.typical_month(db, ym) or {}).get("living")
    pace = insights.living_pace(db, ym, today.day if is_current else None)

    trend = insights.monthly_series(db, ym, 24)
    trend_chart = {
        "type": "bar",
        "labels": [fmt_month(p["month"], short=True) for p in trend],
        "series": [{"name": "Living costs", "values": [p["living"] for p in trend], "color": "--s2"}],
    }
    if typical is not None:
        trend_chart["series"].append(
            {"name": "Typical month", "values": [typical] * len(trend), "color": "--ref", "kind": "line"})

    window_labels = [calendar.month_abbr[int(shift_month(ym, i - 11)[5:])] for i in range(12)]
    multiples = []
    for row in history[:6]:
        months = window_by_key.get(row["key"], {"months": [0] * 12})["months"]
        multiples.append({
            **row,
            "this_month": current.get(row["key"], 0),
            "chart": {"type": "bar", "compact": True, "labels": window_labels,
                      "series": [{"name": row["label"], "values": months, "color": "--s2"}]},
        })

    by_key = {row["key"]: row for row in history}
    table = []
    for key in list(by_key) + [k for k in current if k not in by_key and current[k]]:
        base = by_key.get(key) or {**window_by_key[key], "total": 0, "median": 0}
        table.append({**base, "avg": round(base["total"] / history_months), "this_month": current.get(key, 0)})

    return render_template(
        "living.html",
        ym=ym,
        prev_ym=shift_month(ym, -1),
        next_ym=shift_month(ym, 1) if not is_current else None,
        first_ym=first_ym,
        is_current=is_current,
        day_of_month=today.day,
        month_total=sum(current.values()),
        typical=typical,
        pace=pace,
        history_total=history_total,
        history_months=history_months,
        before_total=before_total,
        trend=trend,
        trend_chart=trend_chart,
        multiples=multiples,
        table=table,
    )


@bp.get("/business")
def businesses_view():
    db = get_db()
    today = now()
    start, end, prev_start, prev_end = insights.year_windows(today.year, today.date())
    rows = []
    for cat in db.execute("SELECT * FROM categories WHERE grp = 'business' ORDER BY label COLLATE NOCASE"):
        all_time = insights.business_totals(db, cat["key"], "0000", "9999")
        if all_time["n"]:
            first = db.execute("SELECT MIN(occurred_at) FROM transactions WHERE category = ?", (cat["key"],)).fetchone()[0]
            rows.append({
                **dict(cat),
                "this_year": insights.business_totals(db, cat["key"], start, end),
                "last_year": insights.business_totals(db, cat["key"], prev_start, prev_end),
                "all_time": all_time,
                "since": first[:7],
            })
    rows.sort(key=lambda row: (-row["this_year"]["income"], -row["all_time"]["income"]))
    return render_template("businesses.html", rows=rows, year=today.year,
                           through=f"{today.day} {calendar.month_abbr[today.month]}")


@bp.get("/business/<key>")
def business_view(key):
    db = get_db()
    cat = db.execute("SELECT * FROM categories WHERE key = ?", (key,)).fetchone()
    if cat is None:
        abort(404)
    today = now()
    try:
        year = int(request.args.get("year", today.year))
    except ValueError:
        year = today.year
    is_current = year == today.year
    months_elapsed = today.month if is_current else 12
    month_names = [calendar.month_abbr[i] for i in range(1, 13)]

    start, end, prev_start, prev_end = insights.year_windows(year, today.date())
    this_year = insights.business_totals(db, key, start, end)
    last_year = insights.business_totals(db, key, prev_start, prev_end)
    all_time = insights.business_totals(db, key, "0000", "9999")
    series = insights.category_series(db, key, f"{year}-12", 12)
    prev_series = insights.category_series(db, key, f"{year - 1}-12", 12)
    years = insights.business_years(db, key)

    def until_now(values):
        return [value if i < months_elapsed else None for i, value in enumerate(values)]

    running = list(itertools.accumulate(p["income"] - p["expense"] for p in series))
    since_labels, since_values = insights.profit_since_start(db, key, month_key(today))
    charts = {
        "monthly": {
            "type": "bar", "labels": month_names, "net": True,
            "series": [
                {"name": "Income", "values": until_now([p["income"] for p in series]), "color": "--s1"},
                {"name": "Costs", "values": until_now([p["expense"] for p in series]), "color": "--s2"},
            ],
        },
        "season": {
            "type": "line", "labels": month_names,
            "series": [{"name": str(year), "values": until_now([p["income"] for p in series]), "color": "--ink"}]
            + ([{"name": str(year - 1), "values": [p["income"] for p in prev_series], "color": "--ref"}]
               if any(p["income"] for p in prev_series) else []),
        },
        "since": {
            "type": "line",
            "labels": [fmt_month(m, short=True) for m in since_labels],
            "series": [{"name": "Profit since the start", "values": since_values, "color": "--ink"}],
        },
    }
    return render_template(
        "business.html",
        cat=cat,
        year=year,
        is_current=is_current,
        through=f"{today.day} {calendar.month_abbr[today.month]}",
        first_year=int(years[0]["year"]) if years else year,
        this_year_number=today.year,
        this_year=this_year,
        last_year=last_year if last_year["n"] else None,
        all_time=all_time,
        since=since_labels[0] if since_labels else None,
        payback=next((since_labels[i] for i in range(1, len(since_values))
                      if since_values[i - 1] < 0 <= since_values[i]), None),
        months=[{**p, "net": p["income"] - p["expense"], "running": running[i]}
                for i, p in enumerate(series[:months_elapsed])],
        month_names=month_names,
        charts=charts,
        years=years,
        largest_income=insights.largest_entries(db, key, start, end, True),
        largest_costs=insights.largest_entries(db, key, start, end, False),
    )


@bp.get("/holdings")
def holdings_view():
    db = get_db()
    moment = now()
    accounts = [holdings.account_view(db, snap) for snap in holdings.latest(db)]
    estimate = liquidity.estimate(db)
    days, balances, checked = liquidity.daily(db, moment.date())
    cash_chart = {
        "type": "line",
        "labels": [holdings.short_date(day) for day in days],
        "series": [
            {"name": "Estimated bank & cash", "values": balances, "color": "--s1"},
            {"name": "Balance checks", "values": checked, "color": "--ink", "kind": "points"},
        ],
    }
    ibkr_total = sum(account["total_cents"] for account in accounts)
    return render_template(
        "holdings.html",
        accounts=accounts,
        configured=ibkr.configured(),
        status=ibkr.status(db),
        estimate=estimate,
        checks=liquidity.checks(db),
        cash_chart=cash_chart,
        ibkr_total=ibkr_total,
        net_worth=(estimate["amount"] if estimate else 0) + ibkr_total,
        default_time=moment.strftime("%Y-%m-%dT%H:%M"),
    )


@bp.post("/holdings/balance")
def holdings_balance():
    db = get_db()
    amount = importer.parse_amount(request.form.get("amount", ""))  # takes 4.520,30 as well as 4520.30
    if amount is None:
        flash("The balance must be a number, e.g. 4520.30")
        return redirect(url_for(".holdings_view"))
    cents = to_cents(amount)
    when = request.form.get("checked_at", "").strip().replace("T", " ")
    when = when + ":00" if len(when) == 16 else (when if len(when) == 19 else ts(now()))
    before, drift = liquidity.record_check(db, cents, "web", session.get("username"),
                                           request.form.get("note", "").strip(), when)
    if before is None or abs(drift) < 100:
        flash("Balance check saved." + (" The estimate matched." if before is not None else ""))
    else:
        flash(f"Balance check saved. The estimate was {fmt_eur(before['amount'])}: "
              f"{fmt_eur(abs(drift))} {'went out' if drift < 0 else 'came in'} without being logged.")
    return redirect(url_for(".holdings_view"))


@bp.post("/holdings/sync")
def holdings_sync():
    db = get_db()
    if not ibkr.configured():
        flash("Set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID in .env first.")
        return redirect(url_for(".holdings_view"))
    last = ibkr.status(db)["last_attempt"]
    if last and last > ts(now() - timedelta(minutes=1)):
        flash("A sync ran less than a minute ago; IBKR allows only a few requests per minute.")
        return redirect(url_for(".holdings_view"))
    days = 365 if request.form.get("history") else None

    def run():
        try:
            ibkr.sync(days=days)
        except Exception:
            pass  # recorded in sync_status and shown on the page

    run_in_background(run)
    flash("Syncing with IBKR. Reload in a minute." if days is None
          else "Loading a year of history from IBKR. Reload in a minute or two.")
    return redirect(url_for(".holdings_view"))


@bp.get("/category/<key>")
def category_view(key):
    db = get_db()
    cat = db.execute("SELECT * FROM categories WHERE key = ?", (key,)).fetchone()
    if cat is None:
        abort(404)
    ym = month_key(now())
    series = insights.category_series(db, key, ym, 24)
    stats = db.execute(
        """SELECT COUNT(*) AS n, MIN(occurred_at) AS first, MAX(occurred_at) AS last,
               COALESCE(SUM(CASE WHEN NOT is_income THEN amount_cents END), 0) AS expense,
               COALESCE(SUM(CASE WHEN is_income THEN amount_cents END), 0) AS income
           FROM transactions WHERE category = ?""",
        (key,),
    ).fetchone()
    year_start = f"{ym[:4]}-01-01"
    ytd = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN NOT is_income THEN amount_cents END), 0) AS expense,
               COALESCE(SUM(CASE WHEN is_income THEN amount_cents END), 0) AS income
           FROM transactions WHERE category = ? AND occurred_at >= ?""",
        (key, year_start),
    ).fetchone()
    recent = db.execute(
        "SELECT * FROM transactions WHERE category = ? ORDER BY occurred_at DESC LIMIT 50", (key,)
    ).fetchall()
    spellings = db.execute(
        "SELECT category_raw, COUNT(*) AS n FROM transactions WHERE category = ? GROUP BY category_raw ORDER BY n DESC",
        (key,),
    ).fetchall()
    aliases = db.execute(
        """SELECT a.alias, (SELECT COUNT(*) FROM transactions t
                            WHERE t.category = a.category_key AND lower(trim(t.category_raw)) = a.alias) AS n
           FROM category_aliases a WHERE a.category_key = ? ORDER BY n DESC, a.alias""",
        (key,),
    ).fetchall()
    return render_template(
        "category.html", cat=cat, series=series, stats=stats, ytd=ytd, recent=recent,
        spellings=spellings, aliases=aliases, groups=GROUPS,
    )


# ── transactions ────────────────────────────────────────────────────────────

def _all_categories(db):
    return db.execute("SELECT key, label FROM categories ORDER BY label COLLATE NOCASE").fetchall()


def _read_transaction_form(form):
    """(values dict, error) from the add/edit form."""
    try:
        cents = to_cents(form.get("amount", ""))
    except (InvalidOperation, ValueError):
        return None, "Amount must be a number, e.g. 12.50"
    category = form.get("category", "").strip()
    when = form.get("occurred_at", "").strip().replace("T", " ")
    if len(when) == 16:
        when += ":00"
    if not category:
        return None, "Category is required."
    if len(when) != 19:
        return None, "Date and time are required."
    return {
        "occurred_at": when,
        "amount_cents": cents,
        "category_raw": category,
        "comment": form.get("comment", "").strip(),
        "is_income": 1 if form.get("type") == "income" else 0,
        "off_balance": 1 if form.get("off_balance") else 0,
    }, None


@bp.get("/transactions")
def transactions():
    db = get_db()
    ym = request.args.get("month", "")
    category = request.args.get("category", "")
    kind = request.args.get("type", "")
    query = request.args.get("q", "").strip()
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1

    clauses, params = [], []
    if valid_month(ym):
        clauses.append("t.occurred_at >= ? AND t.occurred_at < ?")
        params += month_bounds(ym)
    if category:
        clauses.append("t.category = ?")
        params.append(category)
    if kind in ("income", "expense"):
        clauses.append("t.is_income = ?")
        params.append(1 if kind == "income" else 0)
    if query:
        clauses.append("(t.comment LIKE ? OR t.category_raw LIKE ? OR t.user_name LIKE ?)")
        params += [f"%{query}%"] * 3
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    summary = db.execute(
        f"""SELECT COUNT(*) AS n,
               COALESCE(SUM(CASE WHEN t.is_income THEN t.amount_cents END), 0) AS income,
               COALESCE(SUM(CASE WHEN NOT t.is_income THEN t.amount_cents END), 0) AS expense
            FROM transactions t {where}""",
        params,
    ).fetchone()
    rows = db.execute(
        f"""SELECT t.*, c.label FROM transactions t JOIN categories c ON c.key = t.category
            {where} ORDER BY t.occurred_at DESC, t.id DESC LIMIT ? OFFSET ?""",
        [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    return render_template(
        "transactions.html",
        rows=rows,
        summary=summary,
        page=page,
        pages=max((summary["n"] + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        filters={"month": ym if valid_month(ym) else "", "category": category, "type": kind, "q": query},
        all_categories=_all_categories(db),
        default_time=now().strftime("%Y-%m-%dT%H:%M"),
    )


@bp.post("/transactions")
def add_transaction():
    db = get_db()
    values, error = _read_transaction_form(request.form)
    if error:
        flash(error)
        return redirect(url_for(".transactions"))
    stamp = ts(now())
    db.execute(
        """INSERT INTO transactions (occurred_at, user_name, amount_cents, category, category_raw, comment,
               is_income, off_balance, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'web', ?)""",
        (values["occurred_at"], session.get("username"), values["amount_cents"],
         categories.resolve(db, values["category_raw"]), values["category_raw"], values["comment"],
         values["is_income"], values["off_balance"], stamp),
    )
    db.commit()
    flash("Transaction added.")
    return redirect(url_for(".transactions"))


@bp.route("/transactions/<int:tx_id>/edit", methods=["GET", "POST"])
def edit_transaction(tx_id):
    db = get_db()
    row = db.execute(
        "SELECT t.*, c.label FROM transactions t JOIN categories c ON c.key = t.category WHERE t.id = ?", (tx_id,)
    ).fetchone()
    if row is None:
        abort(404)
    back = _back(request.args.get("back"), url_for(".transactions"))
    if request.method == "POST":
        values, error = _read_transaction_form(request.form)
        if error:
            flash(error)
        else:
            db.execute(
                """UPDATE transactions SET occurred_at = ?, amount_cents = ?, category = ?, category_raw = ?,
                       comment = ?, is_income = ?, off_balance = ? WHERE id = ?""",
                (values["occurred_at"], values["amount_cents"], categories.resolve(db, values["category_raw"]),
                 values["category_raw"], values["comment"], values["is_income"], values["off_balance"], tx_id),
            )
            db.commit()
            flash("Transaction saved.")
            return redirect(back)
    return render_template("transaction_edit.html", row=row, all_categories=_all_categories(db), back=back)


@bp.post("/transactions/<int:tx_id>/delete")
def delete_transaction(tx_id):
    db = get_db()
    db.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    db.commit()
    flash("Transaction deleted.")
    return redirect(_back(request.form.get("back"), url_for(".transactions")))


# ── categories ──────────────────────────────────────────────────────────────

@bp.get("/categories")
def categories_view():
    db = get_db()
    since = ts(now() - timedelta(days=365))
    rows = db.execute(
        """SELECT c.*, COUNT(t.id) AS n, MAX(t.occurred_at) AS last_used,
               COALESCE(SUM(CASE WHEN NOT t.is_income AND t.occurred_at >= ? THEN t.amount_cents END), 0) AS spent_year,
               COALESCE(SUM(CASE WHEN t.is_income AND t.occurred_at >= ? THEN t.amount_cents END), 0) AS earned_year,
               (SELECT GROUP_CONCAT(alias, ', ') FROM category_aliases a WHERE a.category_key = c.key) AS aliases
           FROM categories c LEFT JOIN transactions t ON t.category = c.key
           GROUP BY c.key ORDER BY n DESC, c.label COLLATE NOCASE""",
        (since, since),
    ).fetchall()
    return render_template("categories.html", rows=rows, groups=GROUPS)


@bp.post("/categories/<key>")
def update_category(key):
    db = get_db()
    if db.execute("SELECT 1 FROM categories WHERE key = ?", (key,)).fetchone() is None:
        abort(404)
    form = request.form
    budget_text = form.get("budget", "").strip()
    try:
        budget = to_cents(budget_text) if budget_text else None
    except (InvalidOperation, ValueError):
        flash("Budget must be a number.")
        return redirect(_back(form.get("back"), url_for(".categories_view")))
    period = form.get("budget_period") if form.get("budget_period") in ("month", "year") else None
    group = form.get("grp") if form.get("grp") in GROUPS else "living"
    db.execute(
        "UPDATE categories SET label = ?, grp = ?, budget_cents = ?, budget_period = ?, description = ? WHERE key = ?",
        (form.get("label", "").strip() or key, group, budget, period if budget else None,
         form.get("description", "").strip(), key),
    )
    db.commit()
    flash("Category saved.")
    return redirect(_back(form.get("back"), url_for(".categories_view")))


@bp.post("/categories/<key>/merge")
def merge_category(key):
    db = get_db()
    target = request.form.get("target", "")
    exists = db.execute("SELECT COUNT(*) FROM categories WHERE key IN (?, ?)", (key, target)).fetchone()[0]
    if key == target or exists != 2:
        flash("Pick a different, existing category to merge into.")
        return redirect(url_for(".categories_view"))
    categories.merge(db, key, target)
    db.commit()
    flash(f'Merged "{key}" into "{target}". Future messages spelled "{key}" go there too.')
    return redirect(url_for(".categories_view"))


@bp.post("/categories/<key>/split")
def split_category(key):
    db = get_db()
    alias = request.form.get("alias", "")
    owner = db.execute("SELECT category_key FROM category_aliases WHERE alias = ?", (alias,)).fetchone()
    if owner and owner["category_key"] == key and categories.split(db, alias):
        db.commit()
        flash(f'"{alias}" is its own category again.')
    return redirect(url_for(".category_view", key=key))


# ── repeats ─────────────────────────────────────────────────────────────────

@bp.get("/repeats")
def repeats_view():
    db = get_db()
    rows = db.execute(
        """SELECT r.*, (SELECT MAX(due_date) FROM repeat_runs WHERE repeat_id = r.id) AS last_run
           FROM repeats r ORDER BY r.day_of_month, r.id"""
    ).fetchall()
    monthly = sum(row["amount_cents"] * (1 if row["is_income"] else -1) for row in rows)
    return render_template("repeats.html", rows=rows, monthly=monthly, all_categories=_all_categories(db))


@bp.post("/repeats")
def add_repeat():
    db = get_db()
    form = request.form
    try:
        cents = to_cents(form.get("amount", ""))
        day = int(form.get("day", ""))
    except (InvalidOperation, ValueError):
        flash("Amount and day must be numbers.")
        return redirect(url_for(".repeats_view"))
    category = form.get("category", "").strip()
    if not category or not 1 <= day <= 31:
        flash("Category and a day between 1 and 31 are required.")
        return redirect(url_for(".repeats_view"))
    moment = now()
    db.execute(
        """INSERT INTO repeats (user_name, amount_cents, category, comment, is_income, day_of_month,
               active_from, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session.get("username"), cents, category, form.get("comment", "").strip(),
         1 if form.get("type") == "income" else 0, day,
         (moment.date() + timedelta(days=1)).isoformat(), ts(moment)),
    )
    db.commit()
    flash("Repeat added. It is logged from tomorrow on.")
    return redirect(url_for(".repeats_view"))


@bp.post("/repeats/<int:repeat_id>/delete")
def delete_repeat(repeat_id):
    db = get_db()
    db.execute("DELETE FROM repeats WHERE id = ?", (repeat_id,))
    db.commit()
    flash("Repeat deleted. Entries it already logged stay.")
    return redirect(url_for(".repeats_view"))
