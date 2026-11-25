"""RESTORE_MODE: a restored copy runs normally but nothing leaves it.

No digest email, and any mail (login codes) only to RESTORE_MAIL_TO or the
log. Calls to finance's API stay on the compose-internal network, where the
restored finance answers, so they are not switched off.
"""
import logging
import re
from datetime import datetime
from pathlib import Path

import pytest
from conftest import BEN, add_goal, add_task, csrf_from, make_app

from renovation import load_config, mail, maintenance, util
from renovation.db import get_db


def no_network(*args, **kwargs):
    pytest.fail("a restored copy sent mail")


@pytest.fixture
def restored(tmp_path, monkeypatch):
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", no_network)
    return make_app(tmp_path, RESTORE_MODE=True)


@pytest.mark.parametrize("value, on", [("1", True), ("yes", True), ("0", False), ("", False)])
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
    with caplog.at_level(logging.WARNING, logger="renovation.mail"):
        client.post("/login", data={"csrf": csrf_from(client.get("/login")), "email": BEN, "next": ""})
    logged = caplog.records[-1].getMessage()
    assert "RESTORE_MODE: not sent to" in logged and BEN in logged
    code = re.search(r"login code: (\d{6})", logged)[1]
    response = client.post("/login/code", data={"csrf": csrf_from(client.get("/login/code")), "code": code})
    assert response.status_code == 302 and "/login" not in response.headers["Location"]


def test_mail_goes_to_the_redirect_address(restored, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            pass

        def send_message(self, message):
            sent.append(message)

    restored.config["RESTORE_MAIL_TO"] = "drill@example.com"
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    with restored.app_context():
        mail.deliver(mail.build("Ben <ben@example.com>", "Renovation: 2 tasks ready", "Body"))
    [message] = sent
    assert message["To"] == "drill@example.com"
    assert message["Subject"] == "[RESTORE COPY] Renovation: 2 tasks ready"
    assert message["X-Restore-Original-To"] == "Ben <ben@example.com>"


def test_maintenance_skips_the_digest(restored, monkeypatch):
    """Monday 08:01, a digest slot with a task ready: no email, slot left unsent, backup made."""
    goal = add_goal(restored, "Doors", "2027-05-01")
    add_task(restored, goal, "Measure doors", days=1)
    moment = datetime(2026, 11, 23, 8, 1)
    monkeypatch.setattr(util, "now", lambda: moment)
    monkeypatch.setattr(maintenance, "now", lambda: moment)
    monkeypatch.setattr(mail, "deliver", no_network)

    maintenance.tick(restored)

    with restored.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM digest_runs").fetchone()[0] == 0
    assert list(Path(restored.config["BACKUP_DIR"]).glob("renovation-*.db"))
