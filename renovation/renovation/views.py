"""Pages and the chart's JSON. Every view needs a login."""
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from . import ledger, money, plan
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
        "committed": sum(g["committed"] for g in goals),
        "committed_open": sum(g["committed_open"] for g in goals),
        "not_logged": [p for g in goals for p in g["payments"] if p["finance"] == "failed"],
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
        form = {**goal, "budget": plain_amount(goal["budget_cents"])}
        return _goal_page_full(goal, form, [])
    values, errors = _goal_form(request.form)
    if errors:
        return _goal_page(goal, values, errors, 400)
    db = get_db()
    db.execute("UPDATE goals SET title = ?, deadline = ?, budget_cents = ?, notes = ? WHERE id = ?",
               (values["title"], values["deadline"], values["budget_cents"], values["notes"], goal_id))
    db.commit()
    flash(f"Saved {values['title']}.")
    return redirect(_back(url_for(".gantt")))


def _goal_page_full(goal, form, errors, status=200, pay_form=None, pay_errors=(), commit_form=None,
                    commit_errors=()):
    """The big item's page: its form, its tasks, and its money."""
    current = _plan()
    goal_plan = next(g for g in current["goals"] if g["id"] == goal["id"])
    left = sum(p["cents"] for p in money.payments(current) if p["goal_id"] == goal["id"])
    commitment = None
    if pay_form is None:
        pay_form = {"paid_on": today().isoformat(), "log": ledger.enabled()}
        cid = request.args.get("commitment", type=int)
        commitment = next((c for c in goal_plan["commitments"] if c["id"] == cid), None)
        if commitment:
            pay_form.update(commitment_id=commitment["id"], task_id=commitment["task_id"], what=commitment["what"],
                            amount=plain_amount(commitment["open"]))
    return render_template(
        "goal_form.html", goal=goal, form=form, errors=errors, goal_plan=goal_plan, statuses=STATUSES,
        left=left, pay_form=pay_form, pay_errors=pay_errors, commit_form=commit_form or {},
        commit_errors=commit_errors, finance_enabled=ledger.enabled(), paying=commitment,
    ), status


def _unlog(db, where, args):
    """Take payments out of finance before they're deleted here; the reasons it failed."""
    errors = []
    for p in db.execute(f"SELECT * FROM payments WHERE {where} AND finance_key IS NOT NULL", args).fetchall():
        if error := ledger.remove(p["finance_key"]):
            errors.append(error)
    return errors


@bp.post("/goals/<int:goal_id>/delete")
def delete_goal(goal_id):
    goal = _get_goal(goal_id)
    db = get_db()
    if errors := _unlog(db, "goal_id = ?", (goal_id,)):
        flash(f"Nothing deleted: its payments couldn't be taken out of finance ({errors[0]}). Try again later.")
        return redirect(url_for(".edit_goal", goal_id=goal_id))
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
    try:
        values["cost_cents"] = parse_cents(form.get("cost")) or 0
        if values["cost_cents"] < 0:
            errors.append("The estimate can't be negative.")
    except ValueError:
        errors.append("The estimate isn't an amount.")
        values["cost_cents"] = 0

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
    return {**values, "cost": request.form.get("cost", "")}


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
        """INSERT INTO tasks (goal_id, title, days, not_before, status, started_on, done_on, cost_cents,
               pay_at, who, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (values["goal_id"], values["title"], values["days"], values["not_before"], values["status"],
         values["started_on"], values["done_on"], values["cost_cents"], values["pay_at"],
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
        form = {**task, "cost": plain_amount(task["cost_cents"])}
        return _task_page(task, form, needs, [])
    values, needs, errors = _task_form(request.form, task_id)
    if errors:
        return _task_page(task, {**task, **_form_echo(values)}, needs, errors, 400)
    db.execute(
        """UPDATE tasks SET goal_id = ?, title = ?, days = ?, not_before = ?, status = ?, started_on = ?, done_on = ?,
               cost_cents = ?, pay_at = ?, who = ?, notes = ?, updated_at = ? WHERE id = ?""",
        (values["goal_id"], values["title"], values["days"], values["not_before"], values["status"],
         values["started_on"], values["done_on"], values["cost_cents"], values["pay_at"],
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
    """Start, finish or reopen a task. Money is separate: record payments on the big item."""
    day = today()
    if status == "doing":
        started = task["started_on"] or min(scheduled_start, day).isoformat()
        db.execute("UPDATE tasks SET status = 'doing', started_on = ?, done_on = NULL, updated_at = ? WHERE id = ?",
                   (started, ts(now()), task["id"]))
    elif status == "done":
        started = task["started_on"] or min(scheduled_start, day).isoformat()
        db.execute("""UPDATE tasks SET status = 'done', started_on = ?, done_on = ?, updated_at = ?
                      WHERE id = ?""", (started, day.isoformat(), ts(now()), task["id"]))
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


# ── money: payments and commitments ──

def _goal_task_ids(db, goal_id):
    return {r["id"] for r in db.execute("SELECT id FROM tasks WHERE goal_id = ?", (goal_id,))}


def _optional_id(form, field, allowed):
    try:
        value = int(form.get(field) or 0)
    except ValueError:
        return None
    return value if value in allowed else None


def _payment_form(form, goal_id):
    """(values, errors) from the payment form."""
    db = get_db()
    errors = []
    values = {"what": form.get("what", "").strip(), "off_balance": int(bool(form.get("off_balance"))),
              "log": bool(form.get("log"))}
    try:
        values["amount_cents"] = parse_cents(form.get("amount"))
        if not values["amount_cents"]:
            errors.append("How much was paid?")
    except ValueError:
        errors.append("The amount isn't an amount.")
        values["amount_cents"] = None
    try:
        day = parse_day(form.get("paid_on"))
        if day is None:
            errors.append("When was it paid?")
        values["paid_on"] = day.isoformat() if day else ""
    except ValueError:
        errors.append("The day isn't a date.")
        values["paid_on"] = ""
    values["task_id"] = _optional_id(form, "task_id", _goal_task_ids(db, goal_id))
    commitments = {r["id"]: r for r in db.execute("SELECT * FROM commitments WHERE goal_id = ?", (goal_id,))}
    values["commitment_id"] = _optional_id(form, "commitment_id", set(commitments))
    if values["commitment_id"] and values["task_id"] is None:
        values["task_id"] = commitments[values["commitment_id"]]["task_id"]
    if values["log"] and not ledger.enabled():
        values["log"] = False
    return values, errors


def _pay_echo(values):
    return {**values, "amount": request.form.get("amount", "")}


def _after_send(error, what):
    if error:
        flash(f"Saved {what}, but it isn't in finance yet: {error}. Use Retry when finance is back.")
    else:
        flash(f"Saved {what}.")


@bp.post("/goals/<int:goal_id>/payments")
def add_payment(goal_id):
    goal = _get_goal(goal_id)
    values, errors = _payment_form(request.form, goal_id)
    if errors:
        form = {**goal, "budget": plain_amount(goal["budget_cents"])}
        return _goal_page_full(goal, form, [], 400, pay_form=_pay_echo(values), pay_errors=errors)
    db = get_db()
    cur = db.execute(
        """INSERT INTO payments (goal_id, task_id, commitment_id, paid_on, amount_cents, what, off_balance, finance,
               finance_key, user_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (goal_id, values["task_id"], values["commitment_id"], values["paid_on"], values["amount_cents"],
         values["what"], values["off_balance"], "failed" if values["log"] else "skip",
         ledger.new_key() if values["log"] else None, session.get("username"), ledger.stamp()),
    )
    db.commit()
    error = ledger.send(db, cur.lastrowid) if values["log"] else None
    _after_send(error, "the payment" + (" and logged it in finance" if values["log"] and not error else ""))
    return redirect(url_for(".edit_goal", goal_id=goal_id) + "#money")


def _get_payment(payment_id):
    row = get_db().execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if row is None:
        abort(404)
    return dict(row)


@bp.route("/payments/<int:payment_id>", methods=["GET", "POST"])
def edit_payment(payment_id):
    payment = _get_payment(payment_id)
    goal = _get_goal(payment["goal_id"])
    goal_plan = next(g for g in _plan()["goals"] if g["id"] == goal["id"])
    if request.method == "GET":
        form = {**payment, "amount": plain_amount(payment["amount_cents"]), "log": payment["finance"] != "skip"}
        return render_template("payment_form.html", payment=payment, goal=goal, goal_plan=goal_plan, form=form,
                               errors=[], finance_enabled=ledger.enabled())
    values, errors = _payment_form(request.form, goal["id"])
    if errors:
        return render_template("payment_form.html", payment=payment, goal=goal, goal_plan=goal_plan,
                               form=_pay_echo(values), errors=errors, finance_enabled=ledger.enabled()), 400
    db = get_db()
    db.execute(
        """UPDATE payments SET task_id = ?, commitment_id = ?, paid_on = ?, amount_cents = ?, what = ?,
               off_balance = ? WHERE id = ?""",
        (values["task_id"], values["commitment_id"], values["paid_on"], values["amount_cents"], values["what"],
         values["off_balance"], payment_id),
    )
    db.commit()
    error = None
    if values["log"]:
        if not payment["finance_key"]:
            db.execute("UPDATE payments SET finance_key = ? WHERE id = ?", (ledger.new_key(), payment_id))
            db.commit()
        error = ledger.send(db, payment_id)
    elif payment["finance_key"]:
        error = ledger.remove(payment["finance_key"])
        if error:
            flash(f"It's still in finance: {error}.")
            return redirect(url_for(".edit_goal", goal_id=goal["id"]) + "#money")
        db.execute("UPDATE payments SET finance = 'skip', finance_key = NULL, finance_error = NULL WHERE id = ?",
                   (payment_id,))
        db.commit()
    _after_send(error, "the payment")
    return redirect(url_for(".edit_goal", goal_id=goal["id"]) + "#money")


@bp.post("/payments/<int:payment_id>/retry")
def retry_payment(payment_id):
    payment = _get_payment(payment_id)
    if payment["finance"] != "failed":
        return redirect(url_for(".edit_goal", goal_id=payment["goal_id"]) + "#money")
    error = ledger.send(get_db(), payment_id)
    flash(f"Still not in finance: {error}." if error else "Logged in finance.")
    return redirect(_back(url_for(".edit_goal", goal_id=payment["goal_id"]) + "#money"))


@bp.post("/payments/<int:payment_id>/delete")
def delete_payment(payment_id):
    payment = _get_payment(payment_id)
    db = get_db()
    if error := ledger.remove(payment["finance_key"]):
        flash(f"Not deleted: it couldn't be taken out of finance ({error}). Try again later.")
    else:
        db.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
        db.commit()
        flash("Deleted the payment" + (" here and in finance." if payment["finance_key"] else "."))
    return redirect(url_for(".edit_goal", goal_id=payment["goal_id"]) + "#money")


def _commitment_form(form, goal_id):
    errors = []
    values = {"what": form.get("what", "").strip(), "who": form.get("who", "").strip(),
              "notes": form.get("notes", "").strip()}
    if not values["what"]:
        errors.append("What is it for?")
    try:
        values["amount_cents"] = parse_cents(form.get("amount"))
        if not values["amount_cents"] or values["amount_cents"] < 0:
            errors.append("How much was agreed?")
    except ValueError:
        errors.append("The amount isn't an amount.")
        values["amount_cents"] = None
    try:
        day = parse_day(form.get("due_on"))
        values["due_on"] = day.isoformat() if day else None
    except ValueError:
        errors.append("The due day isn't a date.")
        values["due_on"] = None
    values["task_id"] = _optional_id(form, "task_id", _goal_task_ids(get_db(), goal_id))
    return values, errors


@bp.post("/goals/<int:goal_id>/commitments")
def add_commitment(goal_id):
    goal = _get_goal(goal_id)
    values, errors = _commitment_form(request.form, goal_id)
    if errors:
        form = {**goal, "budget": plain_amount(goal["budget_cents"])}
        return _goal_page_full(goal, form, [], 400, commit_form={**values, "amount": request.form.get("amount", "")},
                               commit_errors=errors)
    db = get_db()
    db.execute(
        """INSERT INTO commitments (goal_id, task_id, what, who, amount_cents, due_on, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (goal_id, values["task_id"], values["what"], values["who"], values["amount_cents"], values["due_on"],
         values["notes"], ledger.stamp()),
    )
    db.commit()
    flash(f"Added the commitment: {values['what']}.")
    return redirect(url_for(".edit_goal", goal_id=goal_id) + "#money")


@bp.route("/commitments/<int:commitment_id>", methods=["GET", "POST"])
def edit_commitment(commitment_id):
    db = get_db()
    row = db.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
    if row is None:
        abort(404)
    commitment = dict(row)
    goal = _get_goal(commitment["goal_id"])
    goal_plan = next(g for g in _plan()["goals"] if g["id"] == goal["id"])
    if request.method == "GET":
        form = {**commitment, "amount": plain_amount(commitment["amount_cents"])}
        return render_template("commitment_form.html", commitment=commitment, goal=goal, goal_plan=goal_plan,
                               form=form, errors=[])
    values, errors = _commitment_form(request.form, goal["id"])
    if errors:
        return render_template("commitment_form.html", commitment=commitment, goal=goal, goal_plan=goal_plan,
                               form={**values, "amount": request.form.get("amount", "")}, errors=errors), 400
    db.execute("UPDATE commitments SET task_id = ?, what = ?, who = ?, amount_cents = ?, due_on = ?, notes = ? "
               "WHERE id = ?", (values["task_id"], values["what"], values["who"], values["amount_cents"],
                                values["due_on"], values["notes"], commitment_id))
    db.commit()
    flash(f"Saved the commitment: {values['what']}.")
    return redirect(url_for(".edit_goal", goal_id=goal["id"]) + "#money")


@bp.post("/commitments/<int:commitment_id>/delete")
def delete_commitment(commitment_id):
    db = get_db()
    row = db.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("DELETE FROM commitments WHERE id = ?", (commitment_id,))
    db.commit()
    flash(f"Deleted the commitment: {row['what']}. Payments made towards it stay.")
    return redirect(url_for(".edit_goal", goal_id=row["goal_id"]) + "#money")


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
