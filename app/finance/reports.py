"""The Telegram 'report' (this month by category, as a chart) and 'summary'
(last week or last month in numbers: against the usual level, budgets,
businesses, unusual expenses)."""
import io
import logging
import statistics
from datetime import datetime, time, timedelta

from flask import current_app
from matplotlib.figure import Figure

from . import insights, telegram
from .db import get_db
from .util import fmt_eur, fmt_month, month_bounds, month_key, now, plain_amount, shift_month, ts

log = logging.getLogger(__name__)

TOP_CATEGORIES = 12
INK, INK_MUTED, GRID, SURFACE, BAR = "#0b0b0b", "#52514e", "#e1e0d9", "#fcfcfb", "#2a78d6"


def bar_chart_png(labels, cents, title):
    """Horizontal bars, largest first, each labelled with its amount."""
    values = [c / 100 for c in cents]
    # Figure() without pyplot: no global state, safe in a background thread.
    fig = Figure(figsize=(7, 0.42 * len(values) + 1.3), dpi=150, facecolor=SURFACE)
    ax = fig.add_subplot()
    ax.set_facecolor(SURFACE)
    rows = range(len(values))
    ax.barh(rows, values, height=0.62, color=BAR)
    ax.set_yticks(list(rows), labels, color=INK, fontsize=10)
    ax.invert_yaxis()
    low, high = min(0, *values), max(0, *values)
    span = (high - low) or 1
    ax.set_xlim(low - (0.2 * span if low < 0 else 0), high + 0.22 * span)
    for row, value in zip(rows, values):
        ax.text(value, row, f"  {'−' if value < 0 else ''}€{abs(value):,.2f}  ", va="center",
                ha="left" if value >= 0 else "right",
                fontsize=9, color=INK)
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=10)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="x", colors=INK_MUTED, labelsize=8)
    ax.tick_params(axis="y", length=0)
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color("#c3c2b7")
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    return buffer.getvalue()


def month_report(chat_id):
    ym = month_key(now())
    start, end = month_bounds(ym)
    rows = get_db().execute(
        """SELECT c.label, SUM(t.amount_cents) AS total
           FROM transactions t JOIN categories c ON c.key = t.category
           WHERE NOT t.is_income AND t.occurred_at >= ? AND t.occurred_at < ?
           GROUP BY c.key HAVING total != 0 ORDER BY total DESC""",
        (start, end),
    ).fetchall()
    if not rows:
        telegram.send_text(chat_id, f"No expenses logged for {fmt_month(ym)} yet.")
        return

    labels = [row["label"] for row in rows]
    cents = [row["total"] for row in rows]
    if len(rows) > TOP_CATEGORIES:
        labels = labels[: TOP_CATEGORIES - 1] + [f"{len(rows) - TOP_CATEGORIES + 1} more"]
        cents = cents[: TOP_CATEGORIES - 1] + [sum(cents[TOP_CATEGORIES - 1:])]

    text = "\n".join(f"{row['label']}\t{plain_amount(row['total'])}" for row in rows)
    text += f"\n\nTotal\t{plain_amount(sum(row['total'] for row in rows))}"
    telegram.send_text(chat_id, text)
    telegram.send_photo(chat_id, bar_chart_png(labels, cents, f"Expenses for {fmt_month(ym)}"))
    if current_app.config["PUBLIC_URL"]:
        telegram.send_text(chat_id, f"More in the dashboard: {current_app.config['PUBLIC_URL']}/?month={ym}")


def _changes(current, baseline, labels, min_cents=5000, min_ratio=0.25, limit=3):
    """Categories clearly above / below their usual level: (higher, lower) lists of text lines."""
    rows = []
    for key in set(current) | set(baseline):
        now_total, usual = current.get(key, 0), baseline.get(key, 0)
        change = now_total - usual
        if abs(change) >= min_cents and abs(change) >= min_ratio * max(abs(usual), 1):
            rows.append((change, f"- {labels.get(key, key)} {fmt_eur(change, sign=True)} "
                                 f"({fmt_eur(now_total)} vs usual {fmt_eur(round(usual))})"))
    rows.sort(key=lambda row: row[0])
    higher = [text for change, text in reversed(rows) if change > 0][:limit]
    lower = [text for change, text in rows if change < 0][:limit]
    return higher, lower


def _expense_totals(db, start, end):
    return {key: total for (key, is_income), total in insights.category_totals(db, start, end).items() if not is_income}


def _labels(db):
    return {row["key"]: row["label"] for row in db.execute("SELECT key, label FROM categories")}


def monthly_summary(db, ym, today):
    """Plain-text overview of one month, for Telegram."""
    start, end = month_bounds(ym)
    totals = insights.totals(db, start, end)
    if totals["n"] == 0:
        return f"No entries for {fmt_month(ym)}."
    typical = insights.typical_month(db, ym) or {}
    labels = _labels(db)

    def vs(key):
        return f" (typical {fmt_eur(typical[key])})" if key in typical else ""

    rate = f" · {totals['savings_rate'] * 100:.0f}% kept" if totals["savings_rate"] is not None else ""
    lines = [
        f"Summary · {fmt_month(ym)}",
        "",
        f"Income {fmt_eur(totals['income'])}{vs('income')}",
        f"Spending {fmt_eur(totals['expense'])}{vs('expense')}",
        f"Net {fmt_eur(totals['net'], sign=True)}{rate}",
        f"Living costs {fmt_eur(totals['living'])}{vs('living')}",
    ]

    spending = insights.category_breakdown(db, ym, False)
    if spending:
        lines += ["", "Biggest spending"] + [f"- {row['label']} {fmt_eur(row['total'])}" for row in spending[:5]]

    # Against the monthly average of the year before; categories with nothing
    # this month but a usual level show up as "lower".
    history_start = month_bounds(shift_month(ym, -12))[0]
    months = max(insights.months_with_data(db, history_start, start), 1)
    usual = {key: total / months for key, total in _expense_totals(db, history_start, start).items()}
    higher, lower = _changes({row["key"]: row["total"] for row in spending}, usual, labels)
    if higher:
        lines += ["", "Higher than usual (12-month average)"] + higher
    if lower:
        lines += ["", "Lower than usual"] + lower

    over = [b for b in insights.budgets(db, ym, today.date()) if b["status"] == "over"]
    if over:
        lines += ["", "Over budget"] + [
            f"- {b['label']}: {fmt_eur(b['spent'])} of {fmt_eur(b['budget_cents'])} "
            f"{'this month' if b['budget_period'] == 'month' else 'this year'}"
            for b in over
        ]

    business = [b for b in insights.business_pnl(db, int(ym[:4]), ym) if b["month_net"]]
    if business:
        lines += ["", "Businesses (net this month)", " · ".join(
            f"{b['label']} {fmt_eur(b['month_net'], sign=True)}" for b in business)]

    unusual = insights.unusual_expenses(db, start, end, limit=3)
    if unusual:
        lines += ["", "Unusual"] + [
            f"- {u['occurred_at'][8:10]}.{u['occurred_at'][5:7]}. {u['label']} {fmt_eur(u['amount_cents'])} "
            f"({u['ratio']:.0f}× usual){' ' + u['comment'] if u['comment'] else ''}"
            for u in unusual
        ]

    if current_app.config["PUBLIC_URL"]:
        lines += ["", f"Dashboard: {current_app.config['PUBLIC_URL']}/?month={ym}"]
    return "\n".join(lines)


def weekly_summary(db, today):
    """Plain-text overview of the last 7 days against the 8 weeks before, for Telegram."""
    end = datetime.combine(today.date() + timedelta(days=1), time())
    start = end - timedelta(days=7)
    label = f"last 7 days ({start:%d/%m} - {today:%d/%m/%Y})"
    totals = insights.totals(db, ts(start), ts(end))
    if totals["n"] == 0:
        return f"No entries in the {label}."
    weeks = [(ts(start - timedelta(days=7 * (i + 1))), ts(start - timedelta(days=7 * i))) for i in range(8)]
    history = [insights.totals(db, s, e) for s, e in weeks if insights.has_data(db, s, e)]
    labels = _labels(db)

    def vs(key):
        if not history:
            return ""
        return f" (typical week {fmt_eur(round(statistics.median(h[key] for h in history)))})"

    lines = [
        f"Summary · {label}",
        "",
        f"Spending {fmt_eur(totals['expense'])}{vs('expense')}",
        f"Living costs {fmt_eur(totals['living'])}{vs('living')}",
        f"Income {fmt_eur(totals['income'])}",
    ]
    current = _expense_totals(db, ts(start), ts(end))
    if current:
        top = sorted(current.items(), key=lambda item: -item[1])[:5]
        lines += ["", "Biggest spending"] + [f"- {labels.get(key, key)} {fmt_eur(total)}" for key, total in top]
    if history:
        usual = {}
        for s, e in weeks:
            for key, total in _expense_totals(db, s, e).items():
                usual[key] = usual.get(key, 0) + total / len(weeks)
        higher, _ = _changes(current, usual, labels, min_cents=3000, min_ratio=1.0)
        if higher:
            lines += ["", "Higher than a usual week"] + higher

    largest = insights.largest_expenses(db, ts(start), ts(end), limit=3)
    if largest:
        lines += ["", "Largest expenses"] + [
            f"- {row['occurred_at'][8:10]}.{row['occurred_at'][5:7]}. {row['label']} {fmt_eur(row['amount_cents'])}"
            f"{' ' + row['comment'] if row['comment'] else ''}"
            for row in largest
        ]

    ym = month_key(today)
    pace = insights.living_pace(db, ym, today.day)
    if pace["projected"] is not None:
        lines += ["", f"{fmt_month(ym)} so far: living costs {fmt_eur(pace['current'][today.day - 1])}, "
                      f"on pace for {fmt_eur(pace['projected'])} (typical month {fmt_eur(pace['typical_total'])})"]
    return "\n".join(lines)


def summary_report(chat_id, period):
    db = get_db()
    today = now()
    if period == "weekly":
        text = weekly_summary(db, today)
    else:
        text = monthly_summary(db, shift_month(month_key(today), -1), today)
    telegram.send_long_text(chat_id, text)
