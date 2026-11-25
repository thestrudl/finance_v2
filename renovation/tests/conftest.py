import re
from datetime import date, datetime

import pytest

from renovation import create_app, mail, util
from renovation.db import get_db

BEN = "ben@example.com"
ANA = "ana@example.com"
TODAY = date(2026, 11, 18)


def make_app(tmp_path, **overrides):
    config = {
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE_PATH": str(tmp_path / "renovation.db"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "PUBLIC_URL": "https://renovation.example",
        "PORTAL_USERS": [("Ben Novak", BEN), ("Ana Novak", ANA)],
        "SMTP_HOST": "mail.example",
        "SMTP_PORT": 465,
        "SMTP_USERNAME": "info@example.com",
        "SMTP_PASSWORD": "smtp-password",
        "MAIL_FROM": "Renovation <info@example.com>",
        "FINANCE_API_URL": "http://finance:8000",
        "FINANCE_API_TOKEN": "api-token",
        "FINANCE_CATEGORY": "hisa",
        "SESSION_COOKIE_SECURE": False,
    }
    config.update(overrides)
    return create_app(config)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """Every test runs on 18 November 2026 at noon. Modules that did
    `from .util import now` hold their own reference, so they are frozen too."""
    from renovation import auth, cli, ledger, maintenance, views

    frozen = lambda: datetime(2026, 11, 18, 12, 0)  # noqa: E731
    for module in (util, auth, cli, ledger, maintenance, views):
        monkeypatch.setattr(module, "now", frozen)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def outbox(monkeypatch):
    messages = []
    monkeypatch.setattr(mail, "deliver", messages.append)
    return messages


def csrf_from(response):
    return re.search(r'name="csrf(?:-token)?" (?:value|content)="([^"]+)"', response.get_data(as_text=True))[1]


def login(client, outbox, email=BEN):
    token = csrf_from(client.get("/login"))
    client.post("/login", data={"csrf": token, "email": email, "next": ""})
    code = re.search(r"(\d{6})$", outbox[-1]["Subject"])[1]
    token = csrf_from(client.get("/login/code"))
    return client.post("/login/code", data={"csrf": token, "code": code})


@pytest.fixture
def logged_in(client, outbox):
    login(client, outbox)
    return client


def add_goal(app, title, deadline, budget=None):
    with app.app_context():
        db = get_db()
        cur = db.execute("INSERT INTO goals (title, deadline, budget_cents, created_at) VALUES (?, ?, ?, 'x')",
                         (title, deadline, budget))
        db.commit()
        return cur.lastrowid


def add_task(app, goal_id, title, days=1, needs=(), cost=0, paid=0, pay_at="start", status="todo",
             not_before=None, started_on=None, done_on=None):
    with app.app_context():
        db = get_db()
        cur = db.execute(
            """INSERT INTO tasks (goal_id, title, days, not_before, status, started_on, done_on, cost_cents,
                   pay_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'x', 'x')""",
            (goal_id, title, days, not_before, status, started_on, done_on, cost, pay_at),
        )
        if paid:
            db.execute("""INSERT INTO payments (goal_id, task_id, paid_on, amount_cents, created_at)
                          VALUES (?, ?, '2026-11-01', ?, 'x')""", (goal_id, cur.lastrowid, paid))
        for n in needs:
            db.execute("INSERT INTO task_deps (task_id, needs_id) VALUES (?, ?)", (cur.lastrowid, n))
        db.commit()
        return cur.lastrowid


def build_plan(app):
    from renovation import plan

    with app.app_context():
        return plan.build(get_db(), TODAY)
