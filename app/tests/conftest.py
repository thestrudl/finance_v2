import re
from pathlib import Path

import pytest

from finance import create_app, mail, telegram
from finance.db import get_db

WEBHOOK_PATH = "hook-path"
WEBHOOK_SECRET = "header-secret"
GROUP_CHAT = -100
ADMIN = 1
BEN = "ben@example.com"
ANA = "ana@example.com"


def make_app(tmp_path, **overrides):
    config = {
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE_PATH": str(tmp_path / "finance.db"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "TELEGRAM_BOT_TOKEN": "TOKEN",
        "TELEGRAM_WEBHOOK_PATH": WEBHOOK_PATH,
        "TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "TELEGRAM_ADMIN_ID": str(ADMIN),
        "TELEGRAM_ALLOWED_IDS": {GROUP_CHAT},
        "PUBLIC_URL": "https://finance.example",
        "PORTAL_USERS": [("Ben Novak", BEN), ("Ana Novak", ANA)],
        "SMTP_HOST": "mail.example",
        "SMTP_PORT": 465,
        "SMTP_USERNAME": "info@example.com",
        "SMTP_PASSWORD": "smtp-password",
        "MAIL_FROM": "Finance <info@example.com>",
        "SYNC_TASKS": True,
        "HOUSEHOLD_FILE": str(Path(__file__).with_name("household.json")),
        "SESSION_COOKIE_SECURE": False,
    }
    config.update(overrides)
    return create_app(config)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sent(monkeypatch):
    """Every Telegram API call the app makes, as (method, payload, files)."""
    calls = []

    def fake_api(method, payload=None, files=None):
        calls.append((method, payload or {}, files))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(telegram, "api", fake_api)
    return calls


@pytest.fixture
def outbox(monkeypatch):
    """Every email the app sends, as EmailMessage objects."""
    messages = []
    monkeypatch.setattr(mail, "deliver", messages.append)
    return messages


def texts(calls, chat_id=None):
    return [p["text"] for m, p, _ in calls if m == "sendMessage" and (chat_id is None or p["chat_id"] == chat_id)]


_update_ids = iter(range(1, 10**6))


def message_update(text, chat_id=GROUP_CHAT, user_id=5, first="Ana", last="Novak", update_id=None):
    sender = {"id": user_id, "is_bot": False, "first_name": first}
    if last:
        sender["last_name"] = last
    return {
        "update_id": update_id or next(_update_ids),
        "message": {
            "message_id": 1,
            "from": sender,
            # A group chat has a title but no first/last name.
            "chat": {"id": chat_id, "type": "group", "title": "Household"},
            "date": 0,
            "text": text,
        },
    }


def callback_update(data, chat_id=GROUP_CHAT, user_id=5):
    return {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": "cb1",
            "from": {"id": user_id, "first_name": "Ana"},
            "message": {"message_id": 2, "chat": {"id": chat_id, "type": "group"}},
            "data": data,
        },
    }


def post_update(client, update, secret=WEBHOOK_SECRET, path=WEBHOOK_PATH):
    return client.post(f"/telegram/{path}", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def csrf_from(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.get_data(as_text=True))[1]


def request_code(client, email=BEN, next_url=""):
    token = csrf_from(client.get("/login"))
    return client.post("/login", data={"csrf": token, "email": email, "next": next_url})


def code_from(message):
    return re.search(r"(\d{6})$", message["Subject"])[1]


def link_from(message):
    return re.search(r"https://finance\.example(/login/link\?token=\S+)", message.get_body(("plain",)).get_content())[1]


def enter_code(client, code):
    token = csrf_from(client.get("/login/code"))
    return client.post("/login/code", data={"csrf": token, "code": code})


def login(client, outbox, email=BEN, next_url=""):
    request_code(client, email, next_url)
    return enter_code(client, code_from(outbox[-1]))


def add_tx(app, when, cents, category, is_income=False, comment="", source="web"):
    from finance import categories

    with app.app_context():
        db = get_db()
        cur = db.execute(
            """INSERT INTO transactions (occurred_at, amount_cents, category, category_raw, comment, is_income,
                   source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (when, cents, categories.resolve(db, category), category, comment, int(is_income), source, when),
        )
        db.commit()
        return cur.lastrowid
