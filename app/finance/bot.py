"""Telegram webhook: the Apps Script doPost(), ported."""
import hmac
import logging
import threading
import traceback
from datetime import timedelta

from flask import Blueprint, abort, current_app, request

from . import VERSION, categories, liquidity, reports, telegram
from .db import get_db
from .parser import Parsed, parse
from .util import fmt_eur, now, plain_amount, to_cents, ts

log = logging.getLogger(__name__)

bp = Blueprint("bot", __name__)

WRONG_FORMAT = 'Wrong format. Write "help" if you need help ;)'
UNDO_WINDOW = timedelta(hours=24)


def _same(given, expected):
    return hmac.compare_digest(given.encode(), expected.encode())


@bp.post("/telegram/<path:secret>")
def webhook(secret):
    cfg = current_app.config
    if not cfg["TELEGRAM_WEBHOOK_PATH"] or not _same(secret, cfg["TELEGRAM_WEBHOOK_PATH"]):
        abort(404)
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not cfg["TELEGRAM_WEBHOOK_SECRET"] or not _same(header, cfg["TELEGRAM_WEBHOOK_SECRET"]):
        abort(403)
    update = request.get_json(silent=True)
    if isinstance(update, dict):
        try:
            handle_update(update)
        except Exception:
            log.exception("Telegram update failed")
            telegram.notify_admin("Bot error:\n" + traceback.format_exc()[-3000:])
    # Always 200: anything else makes Telegram deliver the same update again.
    return "", 200


def run_in_background(fn, *args):
    """Slow replies (charts, AI) run after the webhook has answered Telegram."""
    app = current_app._get_current_object()
    if app.config["SYNC_TASKS"]:
        fn(*args)
        return

    def target():
        with app.app_context():
            try:
                fn(*args)
            except Exception:
                log.exception("Background task failed")
                telegram.notify_admin("Bot error:\n" + traceback.format_exc()[-3000:])

    threading.Thread(target=target, daemon=True).start()


def handle_update(update):
    if not _first_delivery(update.get("update_id")):
        return
    if "message" in update:
        _on_message(update["message"])
    elif "callback_query" in update:
        _on_callback(update["callback_query"])


def _first_delivery(update_id):
    if update_id is None:
        return True
    db = get_db()
    cur = db.execute(
        "INSERT OR IGNORE INTO telegram_updates (update_id, received_at) VALUES (?, ?)",
        (update_id, ts(now())),
    )
    db.commit()
    return cur.rowcount == 1


def _authorized(chat_id, user_id):
    cfg = current_app.config
    allowed = set(cfg["TELEGRAM_ALLOWED_IDS"])
    if cfg["TELEGRAM_ADMIN_ID"]:
        allowed.add(int(cfg["TELEGRAM_ADMIN_ID"]))
    return chat_id in allowed or user_id in allowed


def _display_name(sender):
    # The Apps Script read the name off the chat, which in a group has none
    # ("undefined undefined"); the sender is who actually wrote the message.
    name = " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if part)
    return name or sender.get("username") or "unknown"


def _on_message(message):
    text = (message.get("text") or "").strip()
    if not text:
        return  # stickers, photos, members joining
    chat_id = message["chat"]["id"]
    sender = message.get("from") or {}
    if not _authorized(chat_id, sender.get("id")):
        telegram.send_text(chat_id, f"This chat is not set up for the finance bot (chat id {chat_id}).")
        telegram.notify_admin(
            f"Message from a chat that is not allowed: chat {chat_id}, user {sender.get('id')} "
            f"({_display_name(sender)}). Add the id to TELEGRAM_ALLOWED_IDS to let it in.\n\n{text[:200]}"
        )
        return

    parsed = parse(text)
    if parsed.kind in ("expense", "income", "repeat"):
        _store_entry(chat_id, sender, parsed)
    elif parsed.kind == "bad_day":
        example = "repeat in salary 1000 15 company" if parsed.is_income else "repeat Food 5.5 10 comment"
        telegram.send_text(chat_id, f"Specify day of month (1-31) after the amount:\n{example}")
    elif parsed.kind == "in_word":
        telegram.send_text(chat_id, "Income needs a category:\nin CATEGORY AMOUNT COMMENT\nin salary 2500 october")
    elif parsed.kind == "balance":
        cmd_balance(chat_id, sender, parsed.amount)
    elif parsed.kind == "unknown":
        telegram.send_text(chat_id, WRONG_FORMAT)
    else:
        COMMANDS[parsed.kind](chat_id, sender)


def _on_callback(query):
    telegram.answer_callback(query["id"])
    chat = (query.get("message") or {}).get("chat") or {}
    sender = query.get("from") or {}
    chat_id = chat.get("id", sender.get("id"))
    if chat_id is None or not _authorized(chat_id, sender.get("id")):
        return
    data = query.get("data") or ""
    if data.startswith("fix:"):
        _answer_typo(chat_id, data)
        return
    action = CALLBACKS.get(data)
    if action:
        action(chat_id, sender)


def _store_entry(chat_id, sender, parsed):
    """An expense, income or repeat from a message. A category that looks like a
    typo of one in regular use is asked about first."""
    db = get_db()
    name = _display_name(sender)
    suggestion = categories.suggest(db, parsed.category)
    if suggestion:
        _ask_about_typo(db, chat_id, sender, name, parsed, suggestion)
        return
    telegram.send_text(chat_id, _save(db, sender.get("id"), name, parsed, categories.resolve(db, parsed.category)))


def _save(db, sender_id, name, parsed, category_key, shown=None):
    """Store the entry under `category_key`; returns the confirmation text.
    `shown` is the category name to echo back (default: as typed)."""
    moment = now()
    shown = shown or parsed.category
    if parsed.kind == "repeat":
        # Starts tomorrow, like the old 3 a.m. trigger: a repeat set up on its own
        # due day is usually paid and logged by hand that day already.
        db.execute(
            """INSERT INTO repeats (user_name, amount_cents, category, comment, is_income, day_of_month,
                   active_from, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, to_cents(parsed.amount), category_key, parsed.comment, int(parsed.is_income), parsed.day,
             (moment.date() + timedelta(days=1)).isoformat(), ts(moment)),
        )
        db.commit()
        kind = "income" if parsed.is_income else "expense"
        return f"Repeat {kind} stored ({shown}, {parsed.amount}, day {parsed.day}) Thanks {name}"
    db.execute(
        """INSERT INTO transactions (occurred_at, user_name, telegram_user_id, amount_cents, category,
               category_raw, comment, is_income, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'telegram', ?)""",
        (ts(moment), name, sender_id, to_cents(parsed.amount), category_key, parsed.category,
         parsed.comment, int(parsed.is_income), ts(moment)),
    )
    db.commit()
    kind = "Income" if parsed.is_income else "Expense"
    return f"{kind} stored ({shown}, {parsed.amount}, {parsed.comment}) Thanks {name}"


# ── "Did you mean ...?" ─────────────────────────────────────────────────────

TYPO_ANSWER_WITHIN = timedelta(hours=24)


def _label(db, key):
    row = db.execute("SELECT label FROM categories WHERE key = ?", (key,)).fetchone()
    return row["label"] if row else key


def _ask_about_typo(db, chat_id, sender, name, parsed, suggestion):
    cur = db.execute(
        """INSERT INTO pending_entries (chat_id, telegram_user_id, user_name, kind, category_raw, suggestion,
               amount_text, comment, is_income, day_of_month, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (chat_id, sender.get("id"), name, parsed.kind, parsed.category, suggestion, parsed.amount,
         parsed.comment, int(parsed.is_income), parsed.day or None, ts(now())),
    )
    db.commit()
    pending_id = cur.lastrowid
    label = _label(db, suggestion)
    entry = (f"repeat {'in ' if parsed.is_income else ''}{parsed.category} {parsed.amount} day {parsed.day}"
             if parsed.kind == "repeat" else
             f"{'in ' if parsed.is_income else ''}{parsed.category} {parsed.amount} {parsed.comment}".rstrip())
    body = telegram.send_text(
        chat_id,
        f'"{parsed.category}" is not a category yet. Did you mean {label}?\n\n{entry}',
        {"inline_keyboard": [
            [{"text": f"✓ {label}", "callback_data": f"fix:{pending_id}:yes"},
             {"text": f'Keep "{parsed.category}"', "callback_data": f"fix:{pending_id}:keep"}],
            [{"text": "Cancel", "callback_data": f"fix:{pending_id}:cancel"}],
        ]},
    )
    message_id = ((body or {}).get("result") or {}).get("message_id")
    if message_id:
        db.execute("UPDATE pending_entries SET message_id = ? WHERE id = ?", (message_id, pending_id))
        db.commit()


def _answer_typo(chat_id, data):
    try:
        _, pending_id, answer = data.split(":")
    except ValueError:
        return
    db = get_db()
    row = db.execute("SELECT * FROM pending_entries WHERE id = ? AND chat_id = ?", (pending_id, chat_id)).fetchone()
    if row is None:
        return
    # Claim it first, so a double tap stores the entry once.
    claimed = db.execute(
        "UPDATE pending_entries SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL", (ts(now()), row["id"])
    ).rowcount
    db.commit()
    if not claimed:
        return

    if row["created_at"] < ts(now() - TYPO_ANSWER_WITHIN):
        text = "This waited too long and was not stored. Send it again."
    elif answer == "cancel":
        text = "Cancelled, nothing stored."
    else:
        parsed = Parsed(row["kind"], row["category_raw"], row["amount_text"], row["comment"],
                        bool(row["is_income"]), row["day_of_month"] or 0)
        if answer == "yes":
            # Remember the spelling, so it goes straight to the category next time.
            db.execute(
                """INSERT OR REPLACE INTO category_aliases (alias, category_key)
                   SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM categories WHERE key = ?)""",
                (row["category_raw"].lower(), row["suggestion"], row["category_raw"].lower()),
            )
            text = _save(db, row["telegram_user_id"], row["user_name"], parsed, row["suggestion"],
                         shown=_label(db, row["suggestion"]))
        else:
            text = _save(db, row["telegram_user_id"], row["user_name"], parsed,
                         categories.resolve(db, row["category_raw"]))
    if row["message_id"]:
        telegram.edit_text(chat_id, row["message_id"], text)
    else:
        telegram.send_text(chat_id, text)


# ── commands ────────────────────────────────────────────────────────────────

def cmd_help(chat_id, _sender):
    cfg = current_app.config
    text = (
        "Options:\n\n"
        " - help\n"
        " - categories\n"
        " - report  (chart for current month)\n"
        " - summary  (last week or last month in numbers)\n"
        " - list repeat\n"
        " - undo  (remove your last entry)\n"
        " - balance  (estimated bank & cash; 'balance 4520.30' when you know the real total)\n"
        " - dashboard\n\n"
        "Enter expense:\n"
        "CATEGORY X.XX COMMENT\n"
        "Food 5.5 Jurman\n\n"
        'Enter income (add "in"):\n'
        "IN CATEGORY X.XX COMMENT\n"
        "IN Salary 500 Company\n\n"
        "Monthly repeat (DAY = day of month to log):\n"
        "REPEAT (in) CATEGORY X.XX DAY COMMENT\n"
        "REPEAT Food 5.5 10 comment"
    )
    keyboard = [
        [{"text": "Categories", "callback_data": "categories"}],
        [{"text": "Report", "callback_data": "report"}],
        [{"text": "Summary", "callback_data": "summaryMenu"}],
        [{"text": "List Repeat", "callback_data": "listRepeat"}],
    ]
    if cfg["PUBLIC_URL"]:
        keyboard.append([{"text": "Open dashboard", "url": cfg["PUBLIC_URL"]}])
    telegram.send_text(chat_id, text, {"inline_keyboard": keyboard})


def cmd_categories(chat_id, _sender):
    since = ts(now() - timedelta(days=365))
    lines = {True: [], False: []}
    for row in get_db().execute(
        """SELECT c.label, t.is_income, COUNT(*) AS n
           FROM transactions t JOIN categories c ON c.key = t.category
           WHERE t.occurred_at >= ? GROUP BY c.key, t.is_income ORDER BY n DESC""",
        (since,),
    ):
        lines[bool(row["is_income"])].append(f"- {row['label']}")
    telegram.send_text(
        chat_id,
        "Expense categories used in the last year (most used first):\n" + ("\n".join(lines[False]) or "- none")
        + "\n\nIncome categories:\n" + ("\n".join(lines[True]) or "- none")
        + "\n\nAny new word works too; similar spellings can be merged in the dashboard.",
    )


def cmd_report(chat_id, _sender):
    run_in_background(reports.month_report, chat_id)


def cmd_summary_menu(chat_id, _sender):
    telegram.send_text(chat_id, "Choose summary period:", {"inline_keyboard": [
        [{"text": "Last 7 days", "callback_data": "summaryWeekly"}],
        [{"text": "Last month", "callback_data": "summaryMonthly"}],
    ]})


def cmd_summary(period):
    return lambda chat_id, _sender: run_in_background(reports.summary_report, chat_id, period)


def cmd_list_repeat(chat_id, _sender):
    rows = get_db().execute("SELECT * FROM repeats ORDER BY day_of_month, id").fetchall()
    lines = [
        f"Day {r['day_of_month']}: {'in ' if r['is_income'] else ''}{r['category']} "
        f"{plain_amount(r['amount_cents'])} {r['comment']}".rstrip()
        for r in rows
    ]
    url = current_app.config["PUBLIC_URL"]
    footer = f"\n\nEdit or delete them at {url}/repeats" if url else ""
    telegram.send_text(chat_id, ("\n".join(lines) or "No repeat entries.") + footer)


def cmd_version(chat_id, _sender):
    telegram.send_text(chat_id, f"Server backend version: {VERSION}")


def cmd_undo(chat_id, sender):
    db = get_db()
    row = db.execute(
        """SELECT t.*, c.label FROM transactions t JOIN categories c ON c.key = t.category
           WHERE t.source = 'telegram' AND t.telegram_user_id = ? AND t.created_at >= ?
           ORDER BY t.id DESC LIMIT 1""",
        (sender.get("id"), ts(now() - UNDO_WINDOW)),
    ).fetchone()
    if row is None:
        telegram.send_text(chat_id, "Nothing to undo (only your own entries from the last 24 hours).")
        return
    db.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
    db.commit()
    kind = "income" if row["is_income"] else "expense"
    comment = f" ({row['comment']})" if row["comment"] else ""
    telegram.send_text(chat_id, f"Removed {kind}: {row['label']} {plain_amount(row['amount_cents'])}{comment}")


def cmd_balance(chat_id, sender, amount_text=""):
    """Without an amount: the estimate. With one: a balance check."""
    db = get_db()
    if amount_text:
        before, drift = liquidity.record_check(db, to_cents(amount_text), "telegram", _display_name(sender))
        text = f"Balance check stored: {fmt_eur(to_cents(amount_text))} in the accounts and cash."
        if before is not None:
            if abs(drift) < 100:
                text += " The estimate matched."
            elif drift < 0:
                text += (f" The estimate was {fmt_eur(before['amount'])}, so {fmt_eur(-drift)} went out "
                         "without being logged since the last check.")
            else:
                text += (f" The estimate was {fmt_eur(before['amount'])}, so {fmt_eur(drift)} came in "
                         "without being logged since the last check.")
        telegram.send_text(chat_id, text)
        return
    est = liquidity.estimate(db)
    if est is None:
        telegram.send_text(chat_id, "No balance check yet. Send the total in your bank accounts and cash "
                                    "right now, for example:\nbalance 4520.30")
        return
    check = est["check"]
    fund = liquidity.security_fund(db)
    free = ""
    if fund:
        left = est["amount"] - fund
        free = (f"Free to use after the security fund ({fmt_eur(fund)}): {fmt_eur(left)}\n" if left >= 0 else
                f"{fmt_eur(-left)} into the security fund ({fmt_eur(fund)})\n")
    week = liquidity.changes(db, days=(7,)).get(7)
    trend = f"Last 7 days: {fmt_eur(week, sign=True)}\n" if week is not None else ""
    telegram.send_text(
        chat_id,
        f"Estimated bank & cash: {fmt_eur(est['amount'])}\n{free}{trend}"
        f"Balance check {fmt_eur(check['amount_cents'])} on {check['checked_at'][8:10]}.{check['checked_at'][5:7]}., "
        f"then +{fmt_eur(est['income'])} in and −{fmt_eur(est['expense'])} out ({est['entries']} entries).\n"
        "Correct it any time with: balance AMOUNT",
    )


def cmd_dashboard(chat_id, _sender):
    url = current_app.config["PUBLIC_URL"]
    telegram.send_text(chat_id, url or "The dashboard URL is not configured.")


COMMANDS = {
    "help": cmd_help,
    "categories": cmd_categories,
    "report": cmd_report,
    "summary": cmd_summary_menu,
    "list_repeat": cmd_list_repeat,
    "version": cmd_version,
    "undo": cmd_undo,
    "dashboard": cmd_dashboard,
}

CALLBACKS = {
    "categories": cmd_categories,
    "report": cmd_report,
    "listRepeat": cmd_list_repeat,
    "summaryMenu": cmd_summary_menu,
    "summaryWeekly": cmd_summary("weekly"),
    "summaryMonthly": cmd_summary("monthly"),
    # Buttons from old "AI report" messages still in the chat history.
    "aiReportMenu": cmd_summary_menu,
    "aiWeekly": cmd_summary("weekly"),
    "aiMonthly": cmd_summary("monthly"),
}
