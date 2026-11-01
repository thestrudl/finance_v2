"""Liquidity forecast: where bank & cash heads from today's estimate to a chosen day.

Each kind of money is predicted its own way, day by day:
- repeats: exactly as set up, on their day of the month;
- income: what came in on the same days of that month last year (the latest
  full year that has the month), business income included;
- business costs: the same way, so seasonal costs follow seasonal income;
- living costs: the average month of the last 12 full months, day by day
  (big shops early in the month land early). The average, not the median the
  other pages call typical: over several months the odd holiday or car repair
  does happen, and the median would leave it out.

History entries that look like an active repeat (same category, amount and
direction) are left out of the last three, so a repeat isn't counted twice.
Project and savings entries are one-offs and aren't projected: add the ones
you expect as planned entries. Entries outside our accounts never count.
A month with no history at all uses the average month instead.
"""
import calendar
import itertools
import statistics
from collections import defaultdict
from datetime import timedelta

from .categories import lookup
from .util import month_bounds, month_key, shift_month

MAX_DAYS = 3 * 366
ONE_OFF_GROUPS = ("project", "savings")
FROM_LAST_YEAR = ("income", "business")


def days_in(ym):
    return calendar.monthrange(int(ym[:4]), int(ym[5:]))[1]


def source_month(ym, this_ym):
    """The month whose history stands in for `ym`: the same month in the latest
    year before the month in progress."""
    source = shift_month(ym, -12)
    while source >= this_ym:
        source = shift_month(source, -12)
    return source


def history(db, this_ym):
    """({kind: {month: {day: cents}}}, months that have entries) over the 12
    full months before `this_ym`; kinds are income, business and living."""
    start, end = month_bounds(shift_month(this_ym, -12))[0], month_bounds(this_ym)[0]
    repeat_like = {(lookup(db, r["category"]), r["amount_cents"], r["is_income"])
                   for r in db.execute("SELECT category, amount_cents, is_income FROM repeats")}
    kinds = {kind: defaultdict(lambda: defaultdict(int)) for kind in ("income", "business", "living")}
    months = set()
    for row in db.execute(
        """SELECT t.occurred_at, t.amount_cents, t.is_income, t.category, t.source, t.repeat_id, c.grp
           FROM transactions t JOIN categories c ON c.key = t.category
           WHERE NOT t.off_balance AND t.occurred_at >= ? AND t.occurred_at < ?""",
        (start, end),
    ):
        ym, day = row["occurred_at"][:7], int(row["occurred_at"][8:10])
        months.add(ym)
        if (row["repeat_id"] is not None or row["source"] == "repeat"
                or (row["category"], row["amount_cents"], row["is_income"]) in repeat_like):
            continue
        if row["is_income"]:
            kind = None if row["grp"] in ONE_OFF_GROUPS else "income"
        else:
            kind = row["grp"] if row["grp"] in ("business", "living") else None
        if kind:
            kinds[kind][ym][day] += row["amount_cents"]
    return kinds, months


def living_curve(living, months):
    """Average cumulative living costs by day of the month (31 points)."""
    curves = [list(itertools.accumulate(living[ym].get(day, 0) for day in range(1, 32))) for ym in months]
    if not curves:
        return [0] * 31
    return [round(statistics.fmean(curve[d] for curve in curves)) for d in range(31)]


def living_on(curve, day, month_days):
    """Typical living costs on one day; the last day of a short month takes the rest."""
    through = 30 if day == month_days else day - 1
    return curve[through] - (curve[day - 2] if day > 1 else 0)


def build(db, today, until, start, planned=(), fund=0):
    """Day-by-day forecast from tomorrow to `until` (inclusive), starting from
    `start` cents. `planned` holds {"day", "cents" (+ in, - out), "label"}."""
    this_ym = month_key(today)
    kinds, months = history(db, this_ym)
    curve = living_curve(kinds["living"], months)
    average = {kind: round(statistics.fmean(sum(kinds[kind][ym].values()) for ym in months)) if months else 0
               for kind in FROM_LAST_YEAR}
    repeats = db.execute("SELECT * FROM repeats").fetchall()
    planned_on = defaultdict(int)
    for item in planned:
        planned_on[item["day"]] += item["cents"]

    balance, low, below_fund = start, {"day": today, "amount": start}, None
    days, balances, rows, row = [], [], [], None
    day = today + timedelta(days=1)
    while day <= until:
        ym = month_key(day)
        month_days = days_in(ym)
        if row is None or row["month"] != ym:
            source = source_month(ym, this_ym)
            row = {"month": ym, "source": source, "estimated": source not in months, "first": day,
                   "income": 0, "repeat_in": 0, "repeat_out": 0, "living": 0, "business": 0, "planned": 0}
            rows.append(row)
            # Day 31 of the source month lands on the last day of a shorter month.
            by_day = {kind: defaultdict(int) for kind in FROM_LAST_YEAR}
            for kind in FROM_LAST_YEAR:
                for source_day, cents in kinds[kind][source].items():
                    by_day[kind][min(source_day, month_days)] += cents

        moved = {kind: round(average[kind] / month_days) if row["estimated"] else by_day[kind][day.day]
                 for kind in FROM_LAST_YEAR}
        moved["living"] = living_on(curve, day.day, month_days)
        moved["repeat_in"] = moved["repeat_out"] = 0
        for rep in repeats:
            if min(rep["day_of_month"], month_days) == day.day and day.isoformat() >= rep["active_from"]:
                moved["repeat_in" if rep["is_income"] else "repeat_out"] += rep["amount_cents"]
        moved["planned"] = planned_on.get(day, 0)
        for kind, cents in moved.items():
            row[kind] += cents

        balance += (moved["income"] + moved["repeat_in"] + moved["planned"]
                    - moved["repeat_out"] - moved["living"] - moved["business"])
        days.append(day)
        balances.append(balance)
        row["last"], row["end"] = day, balance
        if balance < low["amount"]:
            low = {"day": day, "amount": balance}
        if below_fund is None and fund and balance < fund:
            below_fund = {"day": day, "amount": balance}
        day += timedelta(days=1)

    for row in rows:
        row["net"] = (row["income"] + row["repeat_in"] + row["planned"]
                      - row["repeat_out"] - row["living"] - row["business"])
    return {
        "start": start,
        "end": balance,
        "days": days,
        "balances": balances,
        "months": rows,
        "low": low,
        "below_fund": below_fund,
        "history_months": len(months),
        "average_living": curve[30],
        "average": average,
    }
