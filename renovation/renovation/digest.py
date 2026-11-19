"""The twice-weekly email: what can be worked on now.

Sent to everyone who can log in, at the times in RENOVATION_DIGEST (default
Monday 08:00 and Friday 18:00, in APP_TIMEZONE). The maintenance container
checks every few minutes; each time slot is recorded in digest_runs once sent,
so a restart never sends it twice. A slot missed while the container was down
is still sent within GRACE of its time, not later: a Monday email on Tuesday
would only confuse.

What it lists:
- ready to start: not started, everything it waits for finished, its "not
  before" day reached; most urgent first;
- in progress;
- booked this week: nothing to wait for but a "not before" day within 7 days;
- how many tasks are behind schedule.
"""
import logging
import re
from datetime import datetime, timedelta
from email.utils import formataddr

from flask import current_app, render_template

from . import mail, plan
from .db import get_db
from .util import ts

log = logging.getLogger(__name__)

GRACE = timedelta(hours=6)
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_schedule(text):
    """'mon 08:00, fri 18:00' -> [(0, 8, 0), (4, 18, 0)]. Raises ValueError on junk."""
    slots = []
    for part in (text or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        match = re.fullmatch(r"([a-z]{3})[a-z]*\s+(\d{1,2}):(\d{2})", part)
        if not match or match[1] not in DAYS or int(match[2]) > 23 or int(match[3]) > 59:
            raise ValueError(f"RENOVATION_DIGEST: can't read {part!r} (like 'mon 08:00')")
        slots.append((DAYS[match[1]], int(match[2]), int(match[3])))
    return slots


def describe(schedule):
    """[(0, 8, 0), (4, 18, 0)] -> 'Monday at 08:00 and Friday at 18:00'."""
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    parts = [f"{names[d]} at {h:02d}:{m:02d}" for d, h, m in sorted(schedule)]
    return ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else "".join(parts)


def due_slot(moment, schedule):
    """The latest scheduled time at or before `moment` and within GRACE of it, or None."""
    due = None
    for weekday, hour, minute in schedule:
        day = moment.date() - timedelta(days=(moment.weekday() - weekday) % 7)
        slot = datetime(day.year, day.month, day.day, hour, minute)
        if slot > moment:
            slot -= timedelta(days=7)
        if moment - slot <= GRACE and (due is None or slot > due):
            due = slot
    return due


def content(current, week=7):
    """What the email lists, from a built plan."""
    today = current["today"]
    titles = {g["id"]: g["title"] for g in current["goals"]}
    open_tasks = [t for t in current["tasks"].values() if t["status"] != "done"]
    for t in open_tasks:
        t["goal_title"] = titles[t["goal_id"]]
    unblocked = [t for t in open_tasks if t["status"] == "todo" and not t["blocked_by"]]
    return {
        "today": today,
        "ready": sorted((t for t in unblocked if t["start"] <= today), key=lambda t: (t["slack"], t["start"], t["id"])),
        "doing": sorted((t for t in open_tasks if t["status"] == "doing"), key=lambda t: (t["end"], t["id"])),
        "booked": sorted((t for t in unblocked if today < t["start"] <= today + timedelta(days=week)),
                         key=lambda t: (t["start"], t["id"])),
        "late": sum(t["flag"] == "late" for t in open_tasks),
    }


def subject(lists, moment):
    n = len(lists["ready"])
    when = "this week" if moment.weekday() < 4 else "for the weekend"
    if n:
        return f"Renovation: {n} task{'' if n == 1 else 's'} ready to start {when}"
    return f"Renovation: nothing ready to start {when}"


def send(moment, recipients=None):
    """Email what's ready to every user (or to `recipients`, a list of emails). Returns how many were sent."""
    db = get_db()
    current = plan.build(db, moment.date(), current_app.config["TIGHT_DAYS"])
    lists = content(current)
    users = db.execute("SELECT name, email FROM users ORDER BY id").fetchall()
    if recipients is not None:
        wanted = {r.lower() for r in recipients}
        users = [u for u in users if u["email"].lower() in wanted] + [
            {"name": r, "email": r} for r in wanted - {u["email"].lower() for u in users}]
    base = current_app.config["PUBLIC_URL"]
    links = {"upcoming": f"{base}/upcoming", "plan": f"{base}/", "task": f"{base}/tasks/"}
    title = subject(lists, moment)
    try:
        schedule_text = describe(parse_schedule(current_app.config["DIGEST"])) or "time someone sends it"
    except ValueError:
        schedule_text = "time someone sends it"
    for user in users:
        context = {**lists, "name": user["name"].split()[0], "links": links, "evening": moment.hour >= 12,
                   "schedule": schedule_text}
        mail.deliver(mail.build(
            formataddr((user["name"], user["email"])), title,
            re.sub(r"\n{3,}", "\n\n", render_template("email/digest.txt", **context)),
            render_template("email/digest.html", **context),
        ))
    return len(users)


def run_due(moment):
    """Send the digest if a slot is due and not sent yet. Returns the slot sent, or None."""
    try:
        schedule = parse_schedule(current_app.config["DIGEST"])
    except ValueError:
        log.exception("Digest schedule")
        return None
    slot = due_slot(moment, schedule)
    if slot is None:
        return None
    db = get_db()
    key = ts(slot)
    # Claim the slot first, so two checks at once can't both send it.
    if db.execute("INSERT OR IGNORE INTO digest_runs (slot, sent_at) VALUES (?, ?)", (key, ts(moment))).rowcount == 0:
        db.commit()
        return None
    db.commit()
    try:
        count = send(moment)
    except Exception:
        db.execute("DELETE FROM digest_runs WHERE slot = ?", (key,))  # try again next check
        db.commit()
        raise
    log.info("Digest for %s sent to %d people", key, count)
    return slot
