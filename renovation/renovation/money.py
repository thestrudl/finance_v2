"""Renovation payments laid over the finance forecast.

The finance forecast leaves out Project and Savings one-offs, so it is the
household without the renovation; what's still to pay here is taken off it
day by day. Payments made are in finance already (logged from here, or by
hand), so only what's left counts, in this order:

- commitments (signed offers, orders): what's still open of each, on its due
  day; without one, on the day its task is paid for, else on the deadline;
- task estimates: what isn't paid or committed yet for the task, on its start
  or end day (pay_at). A task marked done has cost what it cost: only its open
  commitments still count;
- payments and commitments on the big item as a whole, not for one task,
  cover the remaining task estimates, earliest first;
- a big item not priced task by task: its rough budget, less what's been paid
  and committed, on its deadline.

A day already past counts tomorrow.
"""
import logging
from collections import defaultdict
from datetime import date, timedelta

import requests
from flask import current_app

log = logging.getLogger(__name__)

MAX_DAYS = 3 * 366


def _pay_day(task):
    return task["start"] if task["pay_at"] == "start" else task["end"]


def payments(plan):
    """Money still to pay, as [{"day", "cents", "label", "goal", "goal_id", "task_id", "kind"
    ("committed" | "estimate" | "budget"), "overdue", "rough"}] by day."""
    first = plan["today"] + timedelta(days=1)
    result = []

    def add(day, cents, label, g, task_id, kind):
        if cents > 0:
            result.append({"day": max(day, first), "cents": cents, "label": label, "goal": g["title"],
                           "goal_id": g["id"], "task_id": task_id, "kind": kind, "overdue": day < first,
                           "rough": kind == "budget"})

    for g in plan["goals"]:
        tasks = {t["id"]: t for t in g["tasks"]}
        for c in g["commitments"]:
            task = tasks.get(c["task_id"])
            day = (date.fromisoformat(c["due_on"]) if c["due_on"]
                   else _pay_day(task) if task else g["deadline_day"])
            add(day, c["open"], c["what"], g, c["task_id"], "committed")

        priced = any(t["cost_cents"] for t in g["tasks"])
        if not priced:
            if g["budget_cents"] and not g["finished"]:
                add(g["deadline_day"], g["budget_cents"] - g["paid"] - g["committed_open"], "Rough budget", g,
                    None, "budget")
            continue

        # The whole item's own payments and open commitments cover task estimates, earliest first.
        cover = (sum(p["amount_cents"] for p in g["payments"] if p["task_id"] not in tasks)
                 + sum(c["open"] for c in g["commitments"] if c["task_id"] not in tasks))
        for t in sorted(g["tasks"], key=lambda t: (_pay_day(t), t["id"])):
            if t["status"] == "done":
                continue
            left = t["cost_cents"] - t["paid"] - t["committed_open"]
            used = min(max(left, 0), max(cover, 0))
            cover -= used
            add(_pay_day(t), left - used, t["title"], g, t["id"], "estimate")
    return sorted(result, key=lambda p: (p["day"], p["goal"], p["label"]))


def horizon(plan, pays):
    """Forecast to a month past the last payment or the last task, at least 3 months."""
    today = plan["today"]
    last = max([p["day"] for p in pays] + [g["end"] for g in plan["goals"] if g["end"]]
               + [g["deadline_day"] for g in plan["goals"] if not g["finished"]], default=today)
    return min(max(last + timedelta(days=31), today + timedelta(days=91)), today + timedelta(days=MAX_DAYS))


def fetch_liquidity(until):
    """(the finance API's answer, None) or (None, why not)."""
    cfg = current_app.config
    if not cfg["FINANCE_API_TOKEN"]:
        return None, "FINANCE_API_TOKEN isn't set, so there's no link to the finance app."
    params = {"until": until.isoformat()}
    if cfg["FINANCE_CATEGORY"]:
        params["category"] = cfg["FINANCE_CATEGORY"]
    try:
        response = requests.get(
            cfg["FINANCE_API_URL"].rstrip("/") + "/api/v1/liquidity", params=params, timeout=8,
            headers={"Authorization": f"Bearer {cfg['FINANCE_API_TOKEN']}"},
        )
        response.raise_for_status()
        return response.json(), None
    except (requests.RequestException, ValueError) as exc:
        log.warning("Finance API failed: %s", exc)
        return None, "The finance app didn't answer, so this page shows the renovation's own numbers only."


def overlay(finance, pays):
    """The finance forecast with and without the renovation, day by day."""
    fc = finance["forecast"]
    days = [date.fromisoformat(d) for d in fc["days"]]
    fund = finance["security_fund_cents"]
    start = finance["balance"]["amount_cents"] if finance["balance"] else 0
    due = defaultdict(int)
    for p in pays:
        due[p["day"]] += p["cents"]

    paid, without, with_reno = 0, [], []
    for day, balance in zip(days, fc["balances"]):
        paid += due.get(day, 0)
        without.append(balance)
        with_reno.append(balance - paid)

    low_index = min(range(len(with_reno)), key=with_reno.__getitem__, default=None)
    low = {"day": days[low_index], "amount": with_reno[low_index]} if low_index is not None else None
    if low is None or start <= low["amount"]:
        low = {"day": date.fromisoformat(finance["today"]), "amount": start}
    below = next(({"day": d, "amount": a} for d, a in zip(days, with_reno) if a < fund), None) if fund else None
    below_zero = next(({"day": d, "amount": a} for d, a in zip(days, with_reno) if a < 0), None)

    months = []
    for i, day in enumerate(days):
        ym = day.strftime("%Y-%m")
        if not months or months[-1]["month"] != ym:
            months.append({"month": ym, "label": day.strftime("%b %Y"), "renovation": 0})
        row = months[-1]
        row["renovation"] += due.get(day, 0)
        row["without"], row["with"] = without[i], with_reno[i]
        row["free"] = with_reno[i] - fund
    return {
        "days": days,
        "without": without,
        "with": with_reno,
        "start": start,
        "fund": fund,
        "low": low,
        # The most that could go elsewhere today (an investment) and still never
        # touch the security fund before the renovation is paid for.
        "spare": low["amount"] - fund,
        "below_fund": below,
        "below_zero": below_zero,
        "end_without": without[-1] if without else start,
        "end_with": with_reno[-1] if with_reno else start,
        "months": months,
        "beyond": sum(p["cents"] for p in pays if days and p["day"] > days[-1]),
    }


def by_month(pays):
    """[(month label, cents, [payments])] for the renovation-only view."""
    groups = defaultdict(list)
    for p in pays:
        groups[p["day"].strftime("%Y-%m")].append(p)
    return [(date.fromisoformat(ym + "-01").strftime("%b %Y"), sum(p["cents"] for p in ps), ps)
            for ym, ps in sorted(groups.items())]
