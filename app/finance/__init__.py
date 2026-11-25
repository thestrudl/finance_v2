"""Household finance: Telegram bot webhook + insights portal.

    /telegram/<secret>   Telegram webhook (same message format as the old Apps Script bot)
    /login               portal login (emailed one-time code)
    /, /year, ...        dashboard, behind login
    /api/v1/...          read-only JSON for the renovation planner (bearer token)
    /healthz             container health check
"""
import hmac
import logging
import os
import secrets
from datetime import timedelta
from email.utils import getaddresses

from flask import Flask, abort, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

VERSION = "2.1"


def _env_ids(name):
    raw = os.environ.get(name, "")
    return {int(part) for part in raw.replace(" ", "").split(",") if part}


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_people(name):
    """'Ana Novak <ana@x.com>, Ben <ben@y.com>' -> [(name, email), ...]"""
    return [(person.strip(), address.strip().lower())
            for person, address in getaddresses([os.environ.get(name, "")]) if "@" in address]


def load_config():
    host = os.environ.get("FINANCE_HOST", "")
    return {
        "SECRET_KEY": os.environ.get("SECRET_KEY", ""),
        "DATABASE_PATH": os.environ.get("DATABASE_PATH", "/data/finance.db"),
        "BACKUP_DIR": os.environ.get("BACKUP_DIR", "/data/backups"),
        "BACKUP_KEEP": int(os.environ.get("BACKUP_KEEP", "14")),
        "APP_TIMEZONE": os.environ.get("APP_TIMEZONE", "Europe/Ljubljana"),
        "PUBLIC_URL": os.environ.get("PUBLIC_URL") or (f"https://{host}" if host else ""),
        "PORTAL_USERS": _env_people("PORTAL_USERS"),
        "SMTP_HOST": os.environ.get("SMTP_HOST", ""),
        "SMTP_PORT": int(os.environ.get("SMTP_PORT", "465")),
        "SMTP_USERNAME": os.environ.get("SMTP_USERNAME", ""),
        "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),
        "MAIL_FROM": os.environ.get("MAIL_FROM", ""),
        "HOUSEHOLD_FILE": os.environ.get("HOUSEHOLD_FILE", ""),  # empty: app/household.json, else the example
        "IBKR_FLEX_TOKEN": os.environ.get("IBKR_FLEX_TOKEN", ""),
        "IBKR_FLEX_QUERY_ID": os.environ.get("IBKR_FLEX_QUERY_ID", ""),
        # Bearer token for /api/v1 (the renovation planner). Empty: no API.
        "FINANCE_API_TOKEN": os.environ.get("FINANCE_API_TOKEN", ""),
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_WEBHOOK_PATH": os.environ.get("TELEGRAM_WEBHOOK_PATH", ""),
        "TELEGRAM_WEBHOOK_SECRET": os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
        "TELEGRAM_ADMIN_ID": os.environ.get("TELEGRAM_ADMIN_ID", ""),
        "TELEGRAM_ALLOWED_IDS": _env_ids("TELEGRAM_ALLOWED_IDS"),
        # Days of the month when bills not logged yet are listed in Telegram.
        "BILL_REMINDER_DAYS": os.environ.get("BILL_REMINDER_DAYS", "10,15,20"),
        "BILL_REMINDER_CHAT": os.environ.get("BILL_REMINDER_CHAT", ""),  # empty: the household group
        # A restored copy (RESTORE.md): the app runs, but nothing leaves it.
        # Mail goes to RESTORE_MAIL_TO or only to the log; the rest is off.
        "RESTORE_MODE": _env_flag("RESTORE_MODE"),
        "RESTORE_MAIL_TO": os.environ.get("RESTORE_MAIL_TO", "").strip(),
        # Tests run background work inline so they can assert on it.
        "SYNC_TASKS": False,
        "SESSION_COOKIE_SECURE": True,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "PERMANENT_SESSION_LIFETIME": timedelta(days=30),
    }


def get_csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(load_config())
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("SECRET_KEY is not set (see .env.example)")
    if app.config["RESTORE_MODE"]:
        logging.getLogger(__name__).warning(
            "RESTORE_MODE is on: no Telegram, no IBKR, no bill reminders; mail goes to %s",
            app.config["RESTORE_MAIL_TO"] or "the log only")

    # Traefik terminates TLS and forwards one hop.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    from . import api, auth, bot, cli, dashboard, db
    from .util import fmt_eur, fmt_month

    db.init_app(app)
    auth.sync_users(app)
    app.register_blueprint(api.bp)
    app.register_blueprint(bot.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    cli.register(app)

    app.jinja_env.globals["csrf_token"] = get_csrf_token
    app.jinja_env.globals["version"] = VERSION
    app.jinja_env.globals["restore_mode"] = app.config["RESTORE_MODE"]
    app.jinja_env.filters["eur"] = fmt_eur
    app.jinja_env.filters["month_name"] = fmt_month

    @app.before_request
    def csrf_protect():
        # The webhook authenticates with Telegram's secret header, the API with its token.
        if request.method == "POST" and request.blueprint not in ("bot", "api"):
            token = session.get("csrf", "")
            sent = request.form.get("csrf", "")
            if not token or not hmac.compare_digest(token.encode(), sent.encode()):
                abort(400, "Form expired, reload the page and try again.")

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'",
        )
        return response

    @app.get("/healthz")
    def healthz():
        db.get_db().execute("SELECT 1")
        return "ok"

    return app
