"""Pages and the chart's JSON. Every view needs a login."""
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from . import money, plan
from .auth import login_required
from .db import get_db
from .util import now, parse_cents, parse_day, plain_amount, today, ts

bp = Blueprint("views", __name__)

STATUSES = {"todo": "To do", "doing": "In progress", "done": "Done"}


@bp.before_request
@login_required
def require_login():
    return None


def _plan():
    return plan.build(get_db(), today(), current_app.config["TIGHT_DAYS"])


def _back(fallback):
    target = request.values.get("next", "")
    return target if target.startswith("/") and not target.startswith("//") else fallback


# ── pages ──

@bp.get("/")
def gantt():
    current = _plan()
    return render_template("gantt.html", plan=current, plan_json=plan.to_json(current),
                           upcoming=plan.upcoming(current), statuses=STATUSES)


@bp.get("/upcoming")
def upcoming():
    current = _plan()
    return render_template("upcoming.html", plan=current, upcoming=plan.upcoming(current), statuses=STATUSES)


@bp.get("/money")
def money_view():
    current = _plan()
    pays = money.payments(current)
    until = money.horizon(current, pays)
    finance, problem = money.fetch_liquidity(until)
    result = money.overlay(finance, pays) if finance else None
    chart = None
    if result:
        labels = [f"{d.day} {d:%b %y}" for d in result["days"]]
        chart = {"type": "line", "labels": labels, "series": [
            {"name": "Without the renovation", "values": result["without"], "color": "--ref"},
            {"name": "With the renovation", "values": result["with"], "color": "--s1"},
        ]}
        if result["fund"]:
            chart["series"].append({"name": "Security fund", "values": [result["fund"]] * len(labels),
                                    "color": "--status-warning", "dash": True})
    goals = current["goals"]
    totals = {
        "estimate": sum(g["estimate"] for g in goals),
        "paid": sum(g["paid"] for g in goals),
        "left": sum(p["cents"] for p in pays),
        "overdue": sum(p["cents"] for p in pays if p["overdue"]),
    }
    return render_template("money.html", plan=current, pays=pays, months=money.by_month(pays), until=until,
                           finance=finance, problem=problem, result=result, chart=chart, totals=totals)


# ── goals ──

def _goal_form(form):
    """(values, errors) from the goal form."""
    errors = []
    values = {"title": form.get("title", "").strip(), "notes": form.get("notes", "").strip()}
    if not values["title"]:
        errors.append("Give it a name.")
    try:
        deadline = parse_day(form.get("deadline"))
        if deadline is None:
            errors.append("Every big item needs a deadline.")
        values["deadline"] = deadline.isoformat() if deadline else ""
    except ValueError:
        errors.append("The deadline isn't a date.")
        values["deadline"] = ""
    try:
        values["budget_cents"] = parse_cents(form.get("budget"))
        if values["budget_cents"] is not None and values["budget_cents"] < 0:
            errors.append("The budget can't be negative.")
    except ValueError:
        errors.append("The budget isn't an amount.")
        values["budget_cents"] = None
    return values, errors


def _goal_page(goal, values, errors, status=200):
    form = {**(goal or {}), **values}
    form["budget"] = request.form.get("budget") if errors else plain_amount(form.get("budget_cents"))
    return render_template("goal_form.html", goal=goal, form=form, errors=errors), status


@bp.route("/goals/new", methods=["GET", "POST"])
def new_goal():
    if request.method == "GET":
        return _goal_page(None, {"deadline": request.args.get("deadline", "")}, [])
    values, errors = _goal_form(request.form)
    if errors:
        return _goal_page(None, values, errors, 400)
    db = get_db()
    cur = db.execute(
        "INSERT INTO goals (title, deadline, budget_cents, notes, created_at) VALUES (?, ?, ?, ?, ?)",
        (values["title"], values["deadline"], values["budget_cents"], values["notes"], ts(now())),
    )
    db.commit()
    flash(f"Added {values['title']}. Now add the work it needs.")
    return redirect(_back(url_for(".gantt")) + f"#goal-{cur.lastrowid}")


def _get_goal(goal_id):
    row = get_db().execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if row is None:
        abort(404)
    return dict(row)


@bp.route("/goals/<int:goal_id>", methods=["GET", "POST"])
def edit_goal(goal_id):
    goal = _get_goal(goal_id)
    if request.method == "GET":
        goal_plan = next(g for g in _plan()["goals"] if g["id"] == goal_id)
        form = {**goal, "budget": plain_amount(goal["budget_cents"])}
        return render_template("goal_form.html", goal=goal, form=form, errors=[], goal_plan=goal_plan,
                               statuses=STATUSES)
    values, errors = _goal_form(request.form)
    if errors:
        return _goal_page(goal, values, errors, 400)
    db = get_db()
    db.execute("UPDATE goals SET title = ?, deadline = ?, budget_cents = ?, notes = ? WHERE id = ?",
               (values["title"], values["deadline"], values["budget_cents"], values["notes"], goal_id))
    db.commit()
    flash(f"Saved {values['title']}.")
    return redirect(_back(url_for(".gantt")))


@bp.post("/goals/<int:goal_id>/delete")
def delete_goal(goal_id):
    goal = _get_goal(goal_id)
    db = get_db()
    db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    db.commit()
    flash(f"Deleted {goal['title']} and the work under it.")
    return redirect(url_for(".gantt"))


# ── tasks ──

def _task_form(form, task_id=None):
    """(values, needs ids, errors) from the task form."""
    db = get_db()
    errors = []
    values = {
        "title": form.get("title", "").strip(),
        "who": form.get("who", "").strip(),
        "notes": form.get("notes", "").strip(),
        "status": form.get("status", "todo") if form.get("status") in STATUSES else "todo",
        "pay_at": "end" if form.get("pay_at") == "end" else "start",
    }
    if not values["title"]:
        errors.append("Give it a name.")
    try:
        values["goal_id"] = int(form.get("goal_id", ""))
        if db.execute("SELECT 1 FROM goals WHERE id = ?", (values["goal_id"],)).fetchone() is None:
            raise ValueError
    except ValueError:
        errors.append("Pick the big item it belongs to.")
        values["goal_id"] = None
    try:
        values["days"] = int(form.get("days", "") or 1)
        if not 1 <= values["days"] <= 3650:
            raise ValueError
    except ValueError:
        errors.append("Days is a whole number from 1 up.")
        values["days"] = 1
    for field, label in (("not_before", "Not before"), ("started_on", "Started on"), ("done_on", "Finished on")):
        try:
            day = parse_day(form.get(field))
            values[field] = day.isoformat() if day else None
        except ValueError:
            errors.append(f"{label} isn't a date.")
            values[field] = None
    for field, label in (("cost_cents", "The estimate"), ("paid_cents", "Paid so far")):
        try:
            cents = parse_cents(form.get(field.removesuffix("_cents"))) or 0
            if cents < 0:
                errors.append(f"{label} can't be negative.")
            values[field] = cents
        except ValueError:
            errors.append(f"{label} isn't an amount.")
            values[field] = 0

    # Status dates: fill in today where they're missing, drop the ones that no longer apply.
    day = today().isoformat()
    if values["status"] == "todo":
        values["started_on"] = values["done_on"] = None
    elif values["status"] == "doing":
        values["started_on"] = values["started_on"] or day
        values["done_on"] = None
    else:
        values["done_on"] = values["done_on"] or day
        if values["started_on"] and values["started_on"] > values["done_on"]:
            errors.append("It can't finish before it started.")

    needs = []
    for raw in form.getlist("needs"):
        try:
            needs_id = int(raw)
        except ValueError:
            continue
        row = db.execute("SELECT title FROM tasks WHERE id = ?", (needs_id,)).fetchone()
        if row is None:
            continue
        if task_id is not None and plan.would_cycle(db, task_id, needs_id):
            errors.append(f"“{row['title']}” already needs this one finished first, so it can't wait for it.")
            continue
        needs.append(needs_id)
    return values, needs, errors


def _task_page(task, form, needs, errors, status=200):
    current = _plan()
    excluded = {task["id"]} if task else set()
    if task:
        # Anything that (further down) needs this task can't also come before it.
        stack = list(current["tasks"][task["id"]]["needed_by"])
        while stack:
            tid = stack.pop()
            if tid not in excluded:
                excluded.add(tid)
                stack.extend(current["tasks"][tid]["needed_by"])
    choices = [(g, [t for t in g["tasks"] if t["id"] not in excluded]) for g in current["goals"]]
    return render_template("task_form.html", task=task, form=form, needs=set(needs), errors=errors,
                           goals=current["goals"], choices=choices, statuses=STATUSES,
                           scheduled=current["tasks"].get(task["id"]) if task else None,
                           next_url=_back("")), status


def _save_needs(db, task_id, needs):
    db.execute("DELETE FROM task_deps WHERE task_id = ?", (task_id,))
    db.executemany("INSERT OR IGNORE INTO task_deps (task_id, needs_id) VALUES (?, ?)",
                   [(task_id, n) for n in needs if n != task_id])


def _form_echo(values):
    """The values as the form shows them again after an error."""
    return {**values, "cost": request.form.get("cost", ""), "paid": request.form.get("paid", "")}


@bp.route("/tasks/new", methods=["GET", "POST"])
def new_task():
    if request.method == "GET":
        form = {"goal_id": request.args.get("goal", type=int), "days": 1, "status": "todo", "pay_at": "start"}
        return _task_page(None, form, request.args.getlist("needs", type=int), [])
    values, needs, errors = _task_form(request.form)
    if errors:
        return _task_page(None, _form_echo(values), needs, errors, 400)
    db = get_db()
    stamp = ts(now())
    cur = db.execute(
        """INSERT INTO tasks (goal_id, title, days, not_before, status, started_on, done_on, cost_cents, paid_cents,
               pay_at, who, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (values["goal_id"], values["title"], values["days"], values["not_before"], values["status"],
         values["started_on"], values["done_on"], values["cost_cents"], values["paid_cents"], values["pay_at"],
         values["who"], values["notes"], stamp, stamp),
    )
    _save_needs(db, cur.lastrowid, needs)
    db.commit()
    flash(f"Added {values['title']}.")
    if request.form.get("another"):
        return redirect(url_for(".new_task", goal=values["goal_id"], next=_back("") or None))
    return redirect(_back(url_for(".gantt")) + f"#task-{cur.lastrowid}")


def _get_task(task_id):
    row = get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        abort(404)
    return dict(row)


@bp.route("/tasks/<int:task_id>", methods=["GET", "POST"])
def edit_task(task_id):
    task = _get_task(task_id)
    db = get_db()
    if request.method == "GET":
        needs = [r["needs_id"] for r in db.execute("SELECT needs_id FROM task_deps WHERE task_id = ?", (task_id,))]
        form = {**task, "cost": plain_amount(task["cost_cents"]), "paid": plain_amount(task["paid_cents"])}
        return _task_page(task, form, needs, [])
    values, needs, errors = _task_form(request.form, task_id)
    if errors:
        return _task_page(task, {**task, **_form_echo(values)}, needs, errors, 400)
    db.execute(
        """UPDATE tasks SET goal_id = ?, title = ?, days = ?, not_before = ?, status = ?, started_on = ?, done_on = ?,
               cost_cents = ?, paid_cents = ?, pay_at = ?, who = ?, notes = ?, updated_at = ? WHERE id = ?""",
        (values["goal_id"], values["title"], values["days"], values["not_before"], values["status"],
         values["started_on"], values["done_on"], values["cost_cents"], values["paid_cents"], values["pay_at"],
         values["who"], values["notes"], ts(now()), task_id),
    )
    _save_needs(db, task_id, needs)
    db.commit()
    flash(f"Saved {values['title']}.")
    return redirect(_back(url_for(".gantt")))


@bp.post("/tasks/<int:task_id>/delete")
def delete_task(task_id):
    task = _get_task(task_id)
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    flash(f"Deleted {task['title']}.")
    return redirect(_back(url_for(".gantt")))


def set_status(db, task, status, scheduled_start):
    """Start, finish or reopen a task. Finishing one with nothing marked as paid
    marks the estimate paid: it's in finance now, not still to come."""
    day = today()
    if status == "doing":
        started = task["started_on"] or min(scheduled_start, day).isoformat()
        db.execute("UPDATE tasks SET status = 'doing', started_on = ?, done_on = NULL, updated_at = ? WHERE id = ?",
                   (started, ts(now()), task["id"]))
    elif status == "done":
        started = task["started_on"] or min(scheduled_start, day).isoformat()
        paid = task["paid_cents"] or task["cost_cents"]
        db.execute("""UPDATE tasks SET status = 'done', started_on = ?, done_on = ?, paid_cents = ?, updated_at = ?
                      WHERE id = ?""", (started, day.isoformat(), paid, ts(now()), task["id"]))
    else:
        db.execute("UPDATE tasks SET status = 'todo', started_on = NULL, done_on = NULL, updated_at = ? WHERE id = ?",
                   (ts(now()), task["id"]))
    db.commit()


@bp.post("/tasks/<int:task_id>/status")
def task_status(task_id):
    task = _get_task(task_id)
    status = request.form.get("status", "")
    if status not in STATUSES:
        abort(400)
    set_status(get_db(), task, status, _plan()["tasks"][task_id]["start"])
    flash({"doing": f"Started {task['title']}.", "done": f"Finished {task['title']}.",
           "todo": f"{task['title']} is back to do."}[status])
    return redirect(_back(url_for(".upcoming")))


# ── chart JSON ──

def _json_error(message, status=400):
    return jsonify({"error": message}), status


@bp.get("/api/plan")
def api_plan():
    return jsonify(plan.to_json(_plan()))


@bp.post("/api/tasks/<int:task_id>/move")
def api_move(task_id):
    """Dragged along the chart: a task to do starts no earlier than the new day
    (what it needs still comes first); one in progress started then."""
    task = _get_task(task_id)
    try:
        start = parse_day((request.get_json(silent=True) or {}).get("start"))
    except (ValueError, TypeError):
        start = None
    if start is None:
        return _json_error("Which day?")
    db = get_db()
    if task["status"] == "done":
        return _json_error("It's finished; edit it to change its dates.")
    if task["status"] == "doing":
        if start > today():
            return _json_error("It's already in progress, so it can't start later than today.")
        db.execute("UPDATE tasks SET started_on = ?, updated_at = ? WHERE id = ?",
                   (start.isoformat(), ts(now()), task_id))
    else:
        db.execute("UPDATE tasks SET not_before = ?, updated_at = ? WHERE id = ?",
                   (start.isoformat() if start > today() else None, ts(now()), task_id))
    db.commit()
    return api_plan()


@bp.post("/api/tasks/<int:task_id>/resize")
def api_resize(task_id):
    task = _get_task(task_id)
    try:
        days = int((request.get_json(silent=True) or {}).get("days"))
    except (ValueError, TypeError):
        return _json_error("How many days?")
    if not 1 <= days <= 3650:
        return _json_error("Days is a whole number from 1 up.")
    if task["status"] == "done":
        return _json_error("It's finished; edit it to change its dates.")
    db = get_db()
    db.execute("UPDATE tasks SET days = ?, updated_at = ? WHERE id = ?", (days, ts(now()), task_id))
    db.commit()
    return api_plan()


@bp.post("/api/tasks/<int:task_id>/needs")
def api_link(task_id):
    """Dragged from one bar's end onto another: task_id can't start before needs_id is finished."""
    _get_task(task_id)
    body = request.get_json(silent=True) or {}
    try:
        needs_id = int(body.get("needs_id"))
    except (ValueError, TypeError):
        return _json_error("Which task?")
    _get_task(needs_id)
    db = get_db()
    if body.get("remove"):
        db.execute("DELETE FROM task_deps WHERE task_id = ? AND needs_id = ?", (task_id, needs_id))
    else:
        if plan.would_cycle(db, task_id, needs_id):
            return _json_error("That would make a loop: the other task already waits for this one.")
        db.execute("INSERT OR IGNORE INTO task_deps (task_id, needs_id) VALUES (?, ?)", (task_id, needs_id))
    db.commit()
    return api_plan()


@bp.post("/api/goals/<int:goal_id>/deadline")
def api_deadline(goal_id):
    _get_goal(goal_id)
    try:
        deadline = parse_day((request.get_json(silent=True) or {}).get("deadline"))
    except (ValueError, TypeError):
        deadline = None
    if deadline is None:
        return _json_error("Which day?")
    db = get_db()
    db.execute("UPDATE goals SET deadline = ? WHERE id = ?", (deadline.isoformat(), goal_id))
    db.commit()
    return api_plan()


@bp.app_template_filter("days_word")
def days_word(n):
    n = abs(n)
    return f"{n} day" if n == 1 else f"{n} days"


@bp.app_template_global()
def relative_day(day):
    """'today', 'tomorrow', 'in 5 days', '3 days ago'."""
    delta = (day - today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    return f"in {delta} days" if delta > 0 else f"{-delta} days ago"


@bp.app_template_global()
def week_of(day):
    return day - timedelta(days=day.weekday())
