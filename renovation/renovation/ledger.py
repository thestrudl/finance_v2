"""Payments logged in finance.

A payment marked to be logged gets a random finance_key and is sent to
finance's PUT /api/v1/transactions/<key> as an expense in
RENOVATION_FINANCE_CATEGORY. Sending the same key again changes that entry,
so a retry never logs it twice, and DELETE removes it. When finance doesn't
answer, the payment is kept here as 'failed' with the reason, to send again.
"""
import logging
import secrets

import requests
from flask import current_app

from .util import now, ts

log = logging.getLogger(__name__)


def enabled():
    cfg = current_app.config
    return bool(cfg["FINANCE_API_TOKEN"] and cfg["FINANCE_CATEGORY"])


def new_key():
    return f"renovation-payment-{secrets.token_hex(8)}"


def _call(method, key, body=None):
    """None if finance did it, else why not."""
    cfg = current_app.config
    try:
        response = requests.request(
            method, f"{cfg['FINANCE_API_URL'].rstrip('/')}/api/v1/transactions/{key}", json=body, timeout=8,
            headers={"Authorization": f"Bearer {cfg['FINANCE_API_TOKEN']}"},
        )
    except requests.RequestException as exc:
        log.warning("Finance %s %s failed: %s", method, key, exc)
        return "finance didn't answer"
    if response.status_code >= 400:
        try:
            reason = response.json().get("error")
        except ValueError:
            reason = None
        log.warning("Finance %s %s: %s %s", method, key, response.status_code, reason)
        return reason or f"finance answered {response.status_code}"
    return None


def comment(goal_title, what, task_title=None):
    detail = what or task_title or ""
    return " · ".join(part for part in ("Renovation", goal_title, detail) if part)


def send(db, payment_id):
    """Log (or update) one payment in finance; returns None or the reason it failed."""
    row = db.execute(
        """SELECT p.*, g.title AS goal_title, t.title AS task_title FROM payments p
           JOIN goals g ON g.id = p.goal_id LEFT JOIN tasks t ON t.id = p.task_id WHERE p.id = ?""",
        (payment_id,),
    ).fetchone()
    if not enabled():
        error = "logging in finance isn't set up (FINANCE_API_TOKEN, RENOVATION_FINANCE_CATEGORY)"
    else:
        error = _call("PUT", row["finance_key"], {
            "day": row["paid_on"],
            "amount_cents": row["amount_cents"],
            "category": current_app.config["FINANCE_CATEGORY"],
            "comment": comment(row["goal_title"], row["what"], row["task_title"]),
            "user_name": row["user_name"] or "",
            "off_balance": bool(row["off_balance"]),
        })
    db.execute("UPDATE payments SET finance = ?, finance_error = ? WHERE id = ?",
               ("failed" if error else "logged", error, payment_id))
    db.commit()
    return error


def finance_transactions():
    """(expenses in RENOVATION_FINANCE_CATEGORY, newest first, None) or (None, why not)."""
    cfg = current_app.config
    if not enabled():
        return None, "linking needs FINANCE_API_TOKEN and RENOVATION_FINANCE_CATEGORY"
    try:
        response = requests.get(
            f"{cfg['FINANCE_API_URL'].rstrip('/')}/api/v1/transactions", params={"category": cfg["FINANCE_CATEGORY"]},
            timeout=8, headers={"Authorization": f"Bearer {cfg['FINANCE_API_TOKEN']}"},
        )
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("Finance transactions failed: %s", exc)
        return None, "finance didn't answer"
    if response.status_code >= 400:
        return None, body.get("error") or f"finance answered {response.status_code}"
    return body["transactions"], None


def refresh_linked(db, transactions):
    """Linked payments follow their finance transaction: amount, day, outside-accounts.
    Returns the linked payments whose transaction is gone from finance."""
    by_id = {t["id"]: t for t in transactions}
    gone = []
    for p in db.execute("SELECT * FROM payments WHERE finance = 'linked'").fetchall():
        t = by_id.get(p["finance_tx_id"])
        if t is None:
            gone.append(dict(p))
        elif (t["amount_cents"], t["occurred_at"][:10], int(t["off_balance"])) != (
                p["amount_cents"], p["paid_on"], p["off_balance"]):
            db.execute("UPDATE payments SET amount_cents = ?, paid_on = ?, off_balance = ? WHERE id = ?",
                       (t["amount_cents"], t["occurred_at"][:10], int(t["off_balance"]), p["id"]))
    db.commit()
    return gone


def remove(key):
    """Take a payment out of finance again; None or the reason it failed."""
    if not key:
        return None
    if not current_app.config["FINANCE_API_TOKEN"]:
        return "FINANCE_API_TOKEN isn't set"
    return _call("DELETE", key)


def stamp():
    return ts(now())
