"""Renovation payments laid over the finance forecast.

The finance forecast leaves out Project and Savings one-offs, so it is the
household without the renovation; the payments still due here are taken off
it day by day. What's already been paid is in today's bank & cash estimate
(logged in finance), so only what's left counts:

- a task not done yet: its estimate minus what's been paid, on its start or
  end day (pay_at); a day already past counts tomorrow;
- a done task: nothing, it's paid;
- a goal not priced task by task yet: its rough budget, on its deadline.
"""
import logging
from collections import defaultdict
from datetime import date, timedelta

import requests
from flask import current_app

log = logging.getLogger(__name__)

MAX_DAYS = 3 * 366


def payments(plan):
    """Money still to pay, as [{"day", "cents", "label", "goal", "overdue", "rough"}] by day."""
    first = plan["today"] + timedelta(days=1)
    result = []
    for g in plan["goals"]:
        priced = any(t["cost_cents"] for t in g["tasks"])
        for t in g["tasks"]:
            left = max(t["cost_cents"] - t["paid_cents"], 0)
            if t["status"] == "done" or not left:
                continue
            day = t["start"] if t["pay_at"] == "start" else t["end"]
            result.append({"day": max(day, first), "cents": left, "label": t["title"], "goal": g["title"],
                           "goal_id": g["id"], "overdue": day < first, "rough": False, "task_id": t["id"]})
        if not priced and g["budget_cents"] and not g["finished"]:
            left = max(g["budget_cents"] - g["paid"], 0)
            if left:
                day = g["deadline_day"]
                result.append({"day": max(day, first), "cents": left, "label": "Rough budget", "goal": g["title"],
                               "goal_id": g["id"], "overdue": day < first, "rough": True, "task_id": None})
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
