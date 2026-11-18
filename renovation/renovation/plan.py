"""The schedule, worked out from goals, tasks and what each task needs first.

Forward pass, in dependency order:
- done:   from started_on (or `days` back from done_on) to done_on;
- doing:  from started_on for `days` days, but never ending before today
          (an overrun stretches the bar until someone marks it done);
- to do:  starts the day after the last thing it needs is finished, not
          before `not_before`, and not before today: whatever hasn't started
          slips, and everything after it slips with it.

Backward pass, from the deadlines: a task must be finished by its goal's
deadline and before anything that needs it has to start, in any goal. The
difference between that latest end and the planned end is the slack:
negative means a deadline will be missed unless something changes ("late"),
a few days or less means there's no room for delays ("tight").

Days are calendar days: weekends count, as they do for most of our own work.
"""
from collections import defaultdict
from datetime import date, timedelta

DAY = timedelta(days=1)


def _day(value):
    return date.fromisoformat(value) if value else None


def load(db):
    goals = [dict(r) for r in db.execute("SELECT * FROM goals")]
    tasks = [dict(r) for r in db.execute("SELECT * FROM tasks")]
    deps = [(r["task_id"], r["needs_id"]) for r in db.execute("SELECT task_id, needs_id FROM task_deps")]
    payments = [dict(r) for r in db.execute("SELECT * FROM payments ORDER BY paid_on, id")]
    commitments = [dict(r) for r in db.execute("SELECT * FROM commitments ORDER BY COALESCE(due_on, '9999'), id")]
    return goals, tasks, deps, payments, commitments


def _order(tasks, needs, dependents):
    """Task ids with everything a task needs before it; ties by id. A cycle
    (refused on save, so only from hand-edited data) is appended as is."""
    waiting = {tid: len(needs[tid]) for tid in tasks}
    ready = sorted(tid for tid, n in waiting.items() if n == 0)
    order = []
    while ready:
        tid = ready.pop(0)
        order.append(tid)
        for nxt in dependents[tid]:
            waiting[nxt] -= 1
            if waiting[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    seen = set(order)
    return order + sorted(tid for tid in tasks if tid not in seen)


def _money(goals, tasks, payments, commitments):
    """Payments and commitments onto their goals and tasks, with totals:
    "paid" (all payments), "committed" (all commitments) and, per commitment,
    what's been paid towards it and what's still "open"."""
    for c in commitments:
        c["paid"] = sum(p["amount_cents"] for p in payments if p["commitment_id"] == c["id"])
        c["open"] = max(c["amount_cents"] - c["paid"], 0)
    for owner in (*goals.values(), *tasks.values()):
        owner["payments"], owner["commitments"] = [], []
    for p in payments:
        goals[p["goal_id"]]["payments"].append(p)
        if p["task_id"] in tasks:
            tasks[p["task_id"]]["payments"].append(p)
    for c in commitments:
        goals[c["goal_id"]]["commitments"].append(c)
        if c["task_id"] in tasks:
            tasks[c["task_id"]]["commitments"].append(c)
    for owner in (*goals.values(), *tasks.values()):
        owner["paid"] = sum(p["amount_cents"] for p in owner["payments"])
        owner["committed"] = sum(c["amount_cents"] for c in owner["commitments"])
        owner["committed_open"] = sum(c["open"] for c in owner["commitments"])


def schedule(goals, tasks, deps, today, tight_days=3, payments=(), commitments=()):
    """{"today", "goals": [goal, ...] by deadline, each with its "tasks", "tasks": {id: task}}."""
    goals = {g["id"]: dict(g) for g in goals}
    tasks = {t["id"]: dict(t) for t in tasks}
    needs, dependents = defaultdict(list), defaultdict(list)
    for task_id, needs_id in deps:
        if task_id in tasks and needs_id in tasks:
            needs[task_id].append(needs_id)
            dependents[needs_id].append(task_id)
    order = _order(tasks, needs, dependents)
    _money(goals, tasks, [dict(p) for p in payments], [dict(c) for c in commitments])

    for tid in order:
        t = tasks[tid]
        span = timedelta(days=t["days"] - 1)
        if t["status"] == "done":
            end = _day(t["done_on"]) or today
            start = min(_day(t["started_on"]) or end - span, end)
        elif t["status"] == "doing":
            start = _day(t["started_on"]) or today
            end = max(start + span, today)
        else:
            after = [tasks[n]["end"] + DAY for n in needs[tid] if "end" in tasks[n]]
            start = max([today, _day(t["not_before"]) or today, *after])
            end = start + span
        t["start"], t["end"] = start, end
        t["needs"] = sorted(needs[tid])
        t["needed_by"] = sorted(dependents[tid])
        t["blocked_by"] = [n for n in t["needs"] if tasks[n]["status"] != "done"]

    for tid in reversed(order):
        t = tasks[tid]
        latest_end = _day(goals[t["goal_id"]]["deadline"])
        for d in dependents[tid]:
            if "latest_start" in tasks[d]:
                latest_end = min(latest_end, tasks[d]["latest_start"] - DAY)
        t["latest_end"] = latest_end
        t["latest_start"] = latest_end - timedelta(days=t["days"] - 1)
        t["slack"] = (latest_end - t["end"]).days
        if t["status"] == "done":
            t["flag"] = ""
        elif t["slack"] < 0:
            t["flag"] = "late"
        elif t["slack"] <= tight_days:
            t["flag"] = "tight"
        else:
            t["flag"] = ""

    by_goal = defaultdict(list)
    for t in tasks.values():
        by_goal[t["goal_id"]].append(t)
    for g in goals.values():
        own = sorted(by_goal[g["id"]], key=lambda t: (t["start"], t["end"], t["id"]))
        deadline = _day(g["deadline"])
        g["deadline_day"] = deadline
        g["tasks"] = own
        g["total"] = len(own)
        g["done"] = sum(t["status"] == "done" for t in own)
        g["finished"] = bool(own) and g["done"] == len(own)
        g["start"] = min((t["start"] for t in own), default=None)
        g["end"] = max((t["end"] for t in own), default=None)
        g["cost"] = sum(t["cost_cents"] for t in own)
        # Until the work is broken down and priced, the rough budget stands in.
        g["estimate"] = g["cost"] if g["cost"] else (g["budget_cents"] or 0)
        g["days_left"] = (deadline - today).days
        g["slack"] = (deadline - g["end"]).days if g["end"] else g["days_left"]
        g["late"] = not g["finished"] and g["slack"] < 0
        g["tight"] = not g["finished"] and not g["late"] and any(t["flag"] == "tight" for t in own)

    ordered = sorted(goals.values(), key=lambda g: (g["finished"], g["deadline"], g["id"]))
    return {"today": today, "goals": ordered, "tasks": tasks}


def build(db, today, tight_days=3):
    goals, tasks, deps, payments, commitments = load(db)
    return schedule(goals, tasks, deps, today, tight_days, payments, commitments)


def would_cycle(db, task_id, needs_id):
    """True if task_id needing needs_id would make a loop (needs_id already
    needs task_id, directly or further down)."""
    if task_id == needs_id:
        return True
    stack, seen = [needs_id], set()
    while stack:
        current = stack.pop()
        if current == task_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(r["needs_id"] for r in db.execute("SELECT needs_id FROM task_deps WHERE task_id = ?", (current,)))
    return False


def upcoming(plan, horizon=30):
    """What the phone view lists: behind schedule, in progress, ready to start,
    and starting within `horizon` days."""
    today = plan["today"]
    goal_titles = {g["id"]: g["title"] for g in plan["goals"]}
    open_tasks = sorted((t for t in plan["tasks"].values() if t["status"] != "done"),
                        key=lambda t: (t["start"], t["slack"], t["id"]))
    for t in open_tasks:
        t["goal_title"] = goal_titles[t["goal_id"]]
    late = sorted((t for t in open_tasks if t["flag"] == "late"), key=lambda t: (t["slack"], t["start"]))
    rest = [t for t in open_tasks if t["flag"] != "late"]
    return {
        "late": late,
        "doing": [t for t in rest if t["status"] == "doing"],
        "ready": [t for t in rest if t["status"] == "todo" and not t["blocked_by"] and t["start"] <= today],
        "soon": [t for t in rest if t["status"] == "todo" and (t["blocked_by"] or t["start"] > today)
                 and t["start"] <= today + timedelta(days=horizon)],
        "horizon": horizon,
    }


def to_json(plan):
    """What the Gantt chart draws."""
    def iso(value):
        return value.isoformat() if value else None

    goals, tasks = [], []
    for g in plan["goals"]:
        goals.append({
            "id": g["id"], "title": g["title"], "deadline": g["deadline"],
            "start": iso(g["start"]), "end": iso(g["end"]), "late": g["late"], "tight": g["tight"],
            "finished": g["finished"], "slack": g["slack"], "done": g["done"], "total": g["total"],
            "estimate": g["estimate"], "paid": g["paid"], "committed": g["committed"], "budget": g["budget_cents"],
            "notes": g["notes"],
        })
        for t in g["tasks"]:
            tasks.append({
                "id": t["id"], "goal_id": t["goal_id"], "title": t["title"], "status": t["status"],
                "start": iso(t["start"]), "end": iso(t["end"]), "days": t["days"],
                "latest_start": iso(t["latest_start"]), "slack": t["slack"], "flag": t["flag"],
                "needs": t["needs"], "blocked_by": t["blocked_by"], "cost": t["cost_cents"],
                "paid": t["paid"], "committed": t["committed"], "who": t["who"], "not_before": t["not_before"], "notes": t["notes"],
            })
    return {"today": plan["today"].isoformat(), "goals": goals, "tasks": tasks}
