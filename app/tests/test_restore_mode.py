"""RESTORE_MODE: a restored copy runs normally but nothing leaves it.

No Telegram (messages, alerts, webhook), no IBKR, no bill reminders, and mail
only to RESTORE_MAIL_TO or the log. Each test makes the real network call
fail loudly, so passing means the call never happened.
"""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import pytest
from conftest import (BEN, GROUP_CHAT, csrf_from, enter_code, make_app, message_update, post_update,
                      request_code)

from finance import bills, categories, ibkr, load_config, mail, scheduler, telegram
from finance.db import get_db


def no_network(*args, **kwargs):
    pytest.fail("a restored copy made a network call")


@pytest.fixture
def restored(tmp_path, monkeypatch):
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", no_network)
    monkeypatch.setattr(telegram.requests, "post", no_network)
    monkeypatch.setattr(ibkr, "fetch_report", no_network)
    return make_app(tmp_path, RESTORE_MODE=True, IBKR_FLEX_TOKEN="tok", IBKR_FLEX_QUERY_ID="999")


class FakeSMTP:
    sent = []

    def __init__(self, host, port, context=None, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.mark.parametrize("value, on", [("1", True), ("true", True), ("on", True), ("0", False), ("", False)])
def test_read_from_the_environment(monkeypatch, value, on):
    monkeypatch.setenv("RESTORE_MODE", value)
    assert load_config()["RESTORE_MODE"] is on


def test_off_by_default(app, client, monkeypatch):
    monkeypatch.delenv("RESTORE_MODE", raising=False)
    assert load_config()["RESTORE_MODE"] is False
    assert app.config["RESTORE_MODE"] is False
    assert "RESTORE COPY" not in client.get("/login").get_data(as_text=True)


def test_banner_on_every_page(restored):
    page = restored.test_client().get("/login").get_data(as_text=True)
    assert "<title>RESTORE COPY · " in page
    assert 'class="restore-banner"' in page


def test_login_code_goes_to_the_log_only(restored, caplog):
    client = restored.test_client()
    with caplog.at_level(logging.WARNING, logger="finance.mail"):
        request_code(client, BEN)
    logged = caplog.records[-1].getMessage()
    assert "RESTORE_MODE: not sent to" in logged and BEN in logged
    code = re.search(r"login code: (\d{6})", logged)[1]
    response = enter_code(client, code)
    assert response.status_code == 302 and "/login" not in response.headers["Location"]


def test_mail_goes_to_the_redirect_address(restored, monkeypatch):
    restored.config["RESTORE_MAIL_TO"] = "drill@example.com"
    FakeSMTP.sent = []
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    with restored.app_context():
        mail.deliver(mail.build("Ben <ben@example.com>", "Finance login code: 123456", "Body"))
    [message] = FakeSMTP.sent
    assert message["To"] == "drill@example.com"
    assert message["Subject"] == "[RESTORE COPY] Finance login code: 123456"
    assert message["X-Restore-Original-To"] == "Ben <ben@example.com>"


def test_bot_stores_entries_but_sends_nothing(restored):
    response = post_update(restored.test_client(), message_update("food 5.5 lunch"))
    assert response.status_code == 200
    with restored.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_telegram_api_refuses(restored):
    with restored.app_context():
        assert telegram.api("sendMessage", {"chat_id": GROUP_CHAT, "text": "hi"}) == {
            "ok": False, "description": telegram.RESTORE_REFUSAL}
        telegram.notify_admin("Scheduler error")


def test_set_webhook_cannot_steal_the_production_bot(restored):
    result = restored.test_cli_runner().invoke(args=["set-webhook"])
    assert result.exit_code != 0
    assert "RESTORE_MODE" in result.output


def test_ibkr_is_never_contacted(restored):
    with restored.app_context():
        assert ibkr.configured()
        assert not ibkr.due(get_db())
        with pytest.raises(ibkr.FlexError, match="RESTORE_MODE"):
            ibkr.sync()
    result = restored.test_cli_runner().invoke(args=["ibkr-sync"])
    assert result.exit_code != 0 and "RESTORE_MODE" in result.output


def test_holdings_sync_button_does_nothing(restored, caplog):
    client = restored.test_client()
    with caplog.at_level(logging.WARNING, logger="finance.mail"):
        request_code(client, BEN)
    enter_code(client, re.search(r"login code: (\d{6})", caplog.records[-1].getMessage())[1])
    token = csrf_from(client.get("/holdings"))
    response = client.post("/holdings/sync", data={"csrf": token}, follow_redirects=True)
    assert "doesn&#39;t contact IBKR" in response.get_data(as_text=True)


def test_scheduler_tick_stays_inside(restored, monkeypatch):
    """A bill reminder day after 9:00 with IBKR configured: only internal jobs run."""
    moment = datetime(2026, 11, 10, 9, 30)
    monkeypatch.setattr(scheduler, "now", lambda: moment)
    monkeypatch.setattr(ibkr, "now", lambda: moment)
    with restored.app_context():
        db = get_db()
        categories.resolve(db, "elektrika")
        db.execute("UPDATE categories SET bill = 1 WHERE key = 'elektrika'")
        db.commit()
        assert bills.reminder_text(db, moment.date())  # a reminder would be due

    scheduler.tick(restored)

    with restored.app_context():
        db = get_db()
        assert db.execute("SELECT 1 FROM settings WHERE key = ?", (bills.SENT_KEY,)).fetchone() is None
        assert db.execute("SELECT 1 FROM sync_status WHERE name = 'ibkr'").fetchone() is None
    assert list((restored.config["BACKUP_DIR"] and __import__("pathlib").Path(restored.config["BACKUP_DIR"])).glob(
        "finance-*.db"))  # the app's own backup still runs


def test_normal_mode_still_reaches_telegram_and_ibkr(app, monkeypatch):
    """The switch is the only difference: with it off, the same calls go out."""
    calls = []
    monkeypatch.setattr(telegram.requests, "post", lambda url, **kw: calls.append(url) or FakeResponse())
    app.config.update(IBKR_FLEX_TOKEN="tok", IBKR_FLEX_QUERY_ID="999")
    monkeypatch.setattr(ibkr, "fetch_report", lambda token, query, days=None: calls.append("ibkr") or ET.fromstring(
        "<FlexQueryResponse/>"))
    with app.app_context():
        telegram.api("sendMessage", {"chat_id": GROUP_CHAT, "text": "hi"})
        with pytest.raises(ibkr.FlexError):
            ibkr.sync()  # empty report, but IBKR was asked
    assert calls[0].startswith("https://api.telegram.org/") and "ibkr" in calls


class FakeResponse:
    def json(self):
        return {"ok": True, "result": {}}
