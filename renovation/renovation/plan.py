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

Waiting can also involve a whole big item: a big item or a task can wait for
another big item (every task in it finished) or for one task. Waiting big
items pass it on to every task in them. These links are expanded into task
links before scheduling, so the passes above handle them like any other.

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
    item_deps = [dict(r) for r in db.execute("SELECT * FROM item_deps ORDER BY id")]
    return goals, tasks, deps, payments, commitments, item_deps


def expand(tasks, deps, item_deps):
    """Every (task, needed task) pair: task links as they are, plus those that
    links involving a big item stand for."""
    by_goal = defaultdict(list)
    for t in tasks:
        by_goal[t["goal_id"]].append(t["id"])
    edges = set(deps)
    for d in item_deps:
        waiting = by_goal[d["goal_id"]] if d["goal_id"] else [d["task_id"]]
        needed = by_goal[d["needs_goal_id"]] if d["needs_goal_id"] else [d["needs_task_id"]]
        edges.update((a, b) for a in waiting for b in needed if a != b)
    return edges


def has_cycle(goal_ids, tasks, deps, item_deps):
    """True if anything ends up waiting for itself. Each big item is a start
    and an end: its tasks wait for its start, its end for its tasks, and its
    end comes after its start, so even big items without tasks yet can't wait
    for each other in a circle."""
    waits = defaultdict(set)
    for g in goal_ids:
        waits[("ge", g)].add(("gs", g))
    for t in tasks:
        waits[("t", t["id"])].add(("gs", t["goal_id"]))
        waits[("ge", t["goal_id"])].add(("t", t["id"]))
    for a, b in deps:
        waits[("t", a)].add(("t", b))
    for d in item_deps:
        node = ("gs", d["goal_id"]) if d["goal_id"] else ("t", d["task_id"])
        waits[node].add(("ge", d["needs_goal_id"]) if d["needs_goal_id"] else ("t", d["needs_task_id"]))
    state = {}  # 1: being visited, 2: done
    for root in list(waits):
        if root in state:
            continue
        stack = [(root, iter(waits[root]))]
        state[root] = 1
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = 2
                stack.pop()
            elif state.get(child) == 1:
                return True
            elif child not in state:
                state[child] = 1
                stack.append((child, iter(waits[child])))
    return False


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


def schedule(goals, tasks, deps, today, tight_days=3, payments=(), commitments=(), item_deps=()):
    """{"today", "goals": [goal, ...] by deadline, each with its "tasks", "tasks": {id: task}}."""
    goals = {g["id"]: dict(g) for g in goals}
    tasks = {t["id"]: dict(t) for t in tasks}
    direct = defaultdict(list)
    for task_id, needs_id in deps:
        if task_id in tasks and needs_id in tasks:
            direct[task_id].append(needs_id)
    item_deps = [dict(d) for d in item_deps]
    needs, dependents = defaultdict(list), defaultdict(list)
    for task_id, needs_id in sorted(expand(tasks.values(), deps, item_deps)):
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
        t["needs"] = sorted(direct[tid])
        t["needed_by"] = sorted(dependents[tid])
        t["blocked_by"] = [n for n in needs[tid] if tasks[n]["status"] != "done"]

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

    # Links involving big items, named, on what waits: for the forms, the chart and "Waiting for".
    for g in goals.values():
        g["item_needs"] = []
    for t in tasks.values():
        t["item_needs"] = []
    for d in item_deps:
        owner = goals.get(d["goal_id"]) if d["goal_id"] else tasks.get(d["task_id"])
        needed = goals.get(d["needs_goal_id"]) if d["needs_goal_id"] else tasks.get(d["needs_task_id"])
        if owner is None or needed is None:
            continue
        d["title"] = needed["title"] + (" (all of it)" if d["needs_goal_id"] else "")
        d["finished"] = needed["finished"] if d["needs_goal_id"] else needed["status"] == "done"
        owner["item_needs"].append(d)
    for t in tasks.values():
        waiting = [tasks[n]["title"] for n in t["needs"] if tasks[n]["status"] != "done"]
        for d in goals[t["goal_id"]]["item_needs"] + t["item_needs"]:
            if not d["finished"] and d["title"] not in waiting:
                waiting.append(d["title"])
        t["waiting_for"] = waiting

    ordered = sorted(goals.values(), key=lambda g: (g["finished"], g["deadline"], g["id"]))
    return {"today": today, "goals": ordered, "tasks": tasks}


def build(db, today, tight_days=3):
    goals, tasks, deps, payments, commitments, item_deps = load(db)
    return schedule(goals, tasks, deps, today, tight_days, payments, commitments, item_deps)


def creates_cycle(db, task_edges=(), item_deps=(), replace_task=None, replace_goal=None):
    """True if adding these links would make something wait for itself.
    replace_task / replace_goal: that one's own links are being replaced, so
    its current ones don't count."""
    goals, tasks, deps, _, _, items = load(db)
    if replace_task is not None:
        deps = [(a, b) for a, b in deps if a != replace_task]
        items = [d for d in items if d["task_id"] != replace_task]
    if replace_goal is not None:
        items = [d for d in items if d["goal_id"] != replace_goal]
    full = [{"goal_id": None, "task_id": None, "needs_goal_id": None, "needs_task_id": None, **d} for d in item_deps]
    return has_cycle([g["id"] for g in goals], tasks, list(deps) + list(task_edges), items + full)


def loop_makers(db, goal_id=None, task_id=None):
    """(goal ids, task ids) that this big item or task can't wait for without
    something ending up waiting for itself: for the forms' choices."""
    goals, tasks, deps, _, _, items = load(db)
    goal_ids = [g["id"] for g in goals]
    if goal_id is not None:
        items = [d for d in items if d["goal_id"] != goal_id]
        owner = {"goal_id": goal_id, "task_id": None}
    else:
        deps = [(a, b) for a, b in deps if a != task_id]
        items = [d for d in items if d["task_id"] != task_id]
        owner = {"goal_id": None, "task_id": task_id}
    bad_goals = {g for g in goal_ids if has_cycle(
        goal_ids, tasks, deps, items + [{**owner, "needs_goal_id": g, "needs_task_id": None}])}
    bad_tasks = {t["id"] for t in tasks if has_cycle(
        goal_ids, tasks, deps, items + [{**owner, "needs_goal_id": None, "needs_task_id": t["id"]}])}
    return bad_goals, bad_tasks


def would_cycle(db, task_id, needs_id):
    """True if task_id waiting for needs_id would make a loop."""
    return task_id == needs_id or creates_cycle(db, task_edges=[(task_id, needs_id)])


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


def _item_json(d):
    return {"id": d["id"], "goal": d["needs_goal_id"], "task": d["needs_task_id"], "finished": d["finished"]}


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
            "notes": g["notes"], "item_needs": [_item_json(d) for d in g["item_needs"]],
        })
        for t in g["tasks"]:
            tasks.append({
                "id": t["id"], "goal_id": t["goal_id"], "title": t["title"], "status": t["status"],
                "start": iso(t["start"]), "end": iso(t["end"]), "days": t["days"],
                "latest_start": iso(t["latest_start"]), "slack": t["slack"], "flag": t["flag"],
                "needs": t["needs"], "blocked_by": t["blocked_by"], "cost": t["cost_cents"],
                "paid": t["paid"], "committed": t["committed"], "who": t["who"], "not_before": t["not_before"], "notes": t["notes"],
                "item_needs": [_item_json(d) for d in t["item_needs"]],
            })
    return {"today": plan["today"].isoformat(), "goals": goals, "tasks": tasks}
