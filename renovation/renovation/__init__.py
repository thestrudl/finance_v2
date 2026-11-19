"""Renovation planner: big items with deadlines, the work under them as a
Gantt chart, and what it all does to our liquidity.

    /login               emailed one-time code, like the finance portal
    /                    Gantt chart (on a phone: what's next)
    /upcoming            what's late, in progress, ready and coming up
    /money               renovation payments over the finance forecast
    /api/plan, ...       JSON behind the chart (drag to move, resize, link)
    /healthz             container health check

Liquidity comes from the finance app's read-only API over the internal Docker
network; nothing here writes to finance.
"""
import hmac
import os
import secrets
from datetime import timedelta
from email.utils import getaddresses

from flask import Flask, abort, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

VERSION = "1.0"


def _env_people(name):
    """'Ana Novak <ana@x.com>, Ben <ben@y.com>' -> [(name, email), ...]"""
    return [(person.strip(), address.strip().lower())
            for person, address in getaddresses([os.environ.get(name, "")]) if "@" in address]


def load_config():
    host = os.environ.get("RENOVATION_HOST", "")
    return {
        # Its own key: the two apps never share sessions.
        "SECRET_KEY": os.environ.get("RENOVATION_SECRET_KEY", ""),
        "DATABASE_PATH": os.environ.get("DATABASE_PATH", "/data/renovation.db"),
        "BACKUP_DIR": os.environ.get("BACKUP_DIR", "/data/backups"),
        "BACKUP_KEEP": int(os.environ.get("BACKUP_KEEP", "14")),
        "APP_TIMEZONE": os.environ.get("APP_TIMEZONE", "Europe/Ljubljana"),
        "PUBLIC_URL": os.environ.get("RENOVATION_PUBLIC_URL") or (f"https://{host}" if host else ""),
        # Who can log in; empty means the same people as the finance portal.
        "PORTAL_USERS": _env_people("RENOVATION_USERS") or _env_people("PORTAL_USERS"),
        "SMTP_HOST": os.environ.get("SMTP_HOST", ""),
        "SMTP_PORT": int(os.environ.get("SMTP_PORT", "465")),
        "SMTP_USERNAME": os.environ.get("SMTP_USERNAME", ""),
        "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),
        "MAIL_FROM": os.environ.get("RENOVATION_MAIL_FROM") or os.environ.get("MAIL_FROM", ""),
        # The finance app's API, reached inside the Docker network.
        "FINANCE_API_URL": os.environ.get("FINANCE_API_URL", "http://finance:8000"),
        "FINANCE_API_TOKEN": os.environ.get("FINANCE_API_TOKEN", ""),
        # The finance category house spending is logged under (Project group).
        "FINANCE_CATEGORY": os.environ.get("RENOVATION_FINANCE_CATEGORY", ""),
        # When the "ready to start" email goes out (APP_TIMEZONE); empty: never.
        "DIGEST": os.environ.get("RENOVATION_DIGEST", "mon 08:00, fri 18:00"),
        # A task this many days or fewer from making its deadline is marked tight.
        "TIGHT_DAYS": int(os.environ.get("RENOVATION_TIGHT_DAYS", "3")),
        "SESSION_COOKIE_SECURE": True,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_NAME": "renovation_session",
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
        raise RuntimeError("RENOVATION_SECRET_KEY is not set (see .env.example)")

    # Traefik terminates TLS and forwards one hop.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    from . import auth, cli, db, views
    from .util import fmt_day, fmt_eur

    db.init_app(app)
    auth.sync_users(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    cli.register(app)

    app.jinja_env.globals["csrf_token"] = get_csrf_token
    app.jinja_env.globals["version"] = VERSION
    app.jinja_env.filters["eur"] = fmt_eur
    app.jinja_env.filters["day"] = fmt_day

    @app.before_request
    def csrf_protect():
        # Forms send the token as a field, the chart's fetch() calls as a header.
        if request.method == "POST":
            token = session.get("csrf", "")
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
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
