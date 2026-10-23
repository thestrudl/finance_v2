"""Passwordless login for the people in PORTAL_USERS.

    /login        enter your email -> we email a 6-digit code and a link
    /login/code   type the code (same browser that asked for it)
    /login/link   open the link, then confirm with a button (mail scanners
                  that prefetch links can't log anyone in)

Codes and links are single-use, expire after CODE_TTL, and allow MAX_ATTEMPTS
guesses. Unknown addresses get the same "check your email" page, minus the email.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from functools import wraps

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from . import mail
from .db import get_db
from .util import now, ts

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)

CODE_TTL = timedelta(minutes=15)
MAX_ATTEMPTS = 5                                # wrong codes before a token is dead
EMAILS_PER_ACCOUNT = (5, timedelta(hours=1))    # login emails per account
REQUESTS_PER_IP = (10, timedelta(hours=1))      # login requests per IP
FAILURES_PER_IP = (10, timedelta(minutes=15))   # wrong codes per IP


def sync_users(app):
    """Make the users table match PORTAL_USERS: add, rename, remove."""
    with app.app_context():
        db = get_db()
        emails = []
        for name, email in app.config["PORTAL_USERS"]:
            emails.append(email.lower())
            db.execute(
                """INSERT INTO users (email, name, created_at) VALUES (?, ?, ?)
                   ON CONFLICT (email) DO UPDATE SET name = excluded.name""",
                (email.lower(), name or email.split("@")[0], ts(now())),
            )
        if emails:
            db.execute(f"DELETE FROM users WHERE email NOT IN ({', '.join('?' * len(emails))})", emails)
        else:
            db.execute("DELETE FROM users")
        db.commit()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        # Re-checked every request, so taking someone off PORTAL_USERS ends their session.
        if user_id and get_db().execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            session.clear()
            user_id = None
        if not user_id:
            # Leave the rest of the session alone: a login may be in progress in another tab.
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else url_for("dashboard.overview")


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _code_hash(link_hash, code):
    # Keyed, so a leaked database doesn't let anyone brute-force live codes offline.
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"{link_hash}:{code}".encode(), hashlib.sha256).hexdigest()


def _ip():
    return request.remote_addr or "unknown"


def _over_limit(db, kind, limit):
    count, window = limit
    return db.execute(
        "SELECT COUNT(*) FROM login_events WHERE ip = ? AND kind = ? AND at >= ?",
        (_ip(), kind, ts(now() - window)),
    ).fetchone()[0] >= count


def _record(db, kind):
    db.execute("INSERT INTO login_events (ip, kind, at) VALUES (?, ?, ?)", (_ip(), kind, ts(now())))
    db.commit()


@bp.get("/login")
def login():
    if session.get("user_id"):
        return redirect(_safe_next(request.args.get("next")))
    return render_template("login.html", next=request.args.get("next", ""))


@bp.post("/login")
def request_code():
    db = get_db()
    email = request.form.get("email", "").strip().lower()
    next_url = request.form.get("next", "")
    if _over_limit(db, "request", REQUESTS_PER_IP):
        flash("Too many login requests from here. Try again in an hour.")
        return render_template("login.html", next=next_url, email=email), 429
    _record(db, "request")

    token_id = 0
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user:
        count, window = EMAILS_PER_ACCOUNT
        recent = db.execute(
            "SELECT COUNT(*) FROM login_tokens WHERE user_id = ? AND created_at >= ?",
            (user["id"], ts(now() - window)),
        ).fetchone()[0]
        if recent >= count:
            flash("Several codes were sent to this address in the last hour. Use the newest one, or wait a bit.")
        else:
            code = f"{secrets.randbelow(10**6):06d}"
            link_token = secrets.token_urlsafe(32)
            link_hash = _digest(link_token)
            moment = now()
            # Only the newest code works.
            db.execute(
                "UPDATE login_tokens SET expires_at = ? WHERE user_id = ? AND used_at IS NULL AND expires_at > ?",
                (ts(moment), user["id"], ts(moment)),
            )
            cur = db.execute(
                """INSERT INTO login_tokens (user_id, code_hash, link_hash, ip, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user["id"], _code_hash(link_hash, code), link_hash, _ip(), ts(moment), ts(moment + CODE_TTL)),
            )
            link = current_app.config["PUBLIC_URL"] + url_for(".link", token=link_token)
            try:
                mail.send_login_email(user, code, link, int(CODE_TTL.total_seconds() // 60))
            except Exception:
                log.exception("Login email to %s failed", email)
                db.rollback()
                flash("The login email could not be sent. Try again in a minute.")
                return render_template("login.html", next=next_url, email=email), 503
            db.commit()
            token_id = cur.lastrowid

    session["pending_login"] = {"token": token_id, "email": email, "next": next_url}
    return redirect(url_for(".code"))


def _start_session(user_id, next_url):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (ts(now()), user_id))
    db.commit()
    session.clear()  # new session id: nothing from before the login carries over
    session.permanent = True
    session["user_id"] = user["id"]
    session["username"] = user["name"]
    session["email"] = user["email"]
    return redirect(_safe_next(next_url))


def _consume(db, token_id):
    """Mark a token used; False if someone else used it first."""
    cur = db.execute(
        "UPDATE login_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL", (ts(now()), token_id)
    )
    db.commit()
    return cur.rowcount == 1


def _live_token(db, where, value):
    return db.execute(
        f"""SELECT t.*, u.name FROM login_tokens t JOIN users u ON u.id = t.user_id
            WHERE {where} = ? AND t.used_at IS NULL AND t.expires_at > ? AND t.attempts < ?""",
        (value, ts(now()), MAX_ATTEMPTS),
    ).fetchone()


@bp.route("/login/code", methods=["GET", "POST"])
def code():
    pending = session.get("pending_login")
    if not pending:
        return redirect(url_for(".login"))
    if request.method == "GET":
        return render_template("login_code.html", email=pending["email"])

    db = get_db()
    if _over_limit(db, "failure", FAILURES_PER_IP):
        flash("Too many wrong codes. Try again in 15 minutes.")
        return render_template("login_code.html", email=pending["email"]), 429

    entered = "".join(ch for ch in request.form.get("code", "") if ch.isdigit())
    token = _live_token(db, "t.id", pending["token"]) if pending["token"] else None
    if token and hmac.compare_digest(token["code_hash"], _code_hash(token["link_hash"], entered)):
        if _consume(db, token["id"]):
            return _start_session(token["user_id"], pending.get("next"))
    elif token:
        db.execute("UPDATE login_tokens SET attempts = attempts + 1 WHERE id = ?", (token["id"],))
    _record(db, "failure")
    flash("That code is wrong or has expired.")
    return render_template("login_code.html", email=pending["email"]), 401


@bp.get("/login/link")
def link():
    token = _live_token(get_db(), "t.link_hash", _digest(request.args.get("token", "")))
    if token is None:
        flash("That login link has expired or was already used. Ask for a new one.")
        return redirect(url_for(".login"))
    return render_template("login_link.html", token=request.args["token"], name=token["name"])


@bp.post("/login/link")
def link_confirm():
    db = get_db()
    token = _live_token(db, "t.link_hash", _digest(request.form.get("token", "")))
    if token is None or not _consume(db, token["id"]):
        flash("That login link has expired or was already used. Ask for a new one.")
        return redirect(url_for(".login"))
    return _start_session(token["user_id"], None)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
