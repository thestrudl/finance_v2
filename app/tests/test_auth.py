import ssl

from conftest import (ANA, BEN, code_from, csrf_from, enter_code, link_from, login, make_app, request_code)

from finance import mail
from finance.db import get_db


def logged_in_as(client):
    response = client.get("/")
    return response.status_code == 200 and response.get_data(as_text=True)


def test_users_come_from_config(app):
    with app.app_context():
        users = get_db().execute("SELECT name, email FROM users ORDER BY id").fetchall()
    assert [tuple(u) for u in users] == [("Ben Novak", BEN), ("Ana Novak", ANA)]


def test_removing_a_user_from_config_deletes_them(tmp_path):
    make_app(tmp_path)
    app = make_app(tmp_path, PORTAL_USERS=[("Ben Novak", BEN)])
    with app.app_context():
        assert [r[0] for r in get_db().execute("SELECT email FROM users")] == [BEN]


def test_code_login(client, outbox):
    response = request_code(client)
    assert response.headers["Location"] == "/login/code"
    [message] = outbox
    assert message["To"] == f"Ben Novak <{BEN}>"
    assert message["From"] == "Finance <info@example.com>"
    code = code_from(message)
    body = message.get_body(("plain",)).get_content()
    assert code in body and "Hi Ben," in body
    assert code in message.get_body(("html",)).get_content()

    response = enter_code(client, code[:3] + " " + code[3:])  # spaces are fine
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert "Ben Novak" in logged_in_as(client)


def test_login_returns_to_the_page_asked_for(client, outbox):
    login_page = client.get(client.get("/year?year=2025").headers["Location"])
    assert 'name="next" value="/year?year=2025"' in login_page.get_data(as_text=True)
    response = login(client, outbox, next_url="/year?year=2025")
    assert response.headers["Location"] == "/year?year=2025"


def test_offsite_next_is_ignored(client, outbox):
    assert login(client, outbox, next_url="//evil.example/").headers["Location"] == "/"


def test_unknown_address_looks_the_same_but_gets_no_email(client, outbox):
    response = request_code(client, "stranger@example.com")
    assert response.headers["Location"] == "/login/code"
    assert outbox == []
    assert enter_code(client, "123456").status_code == 401
    assert not logged_in_as(client)


def test_email_address_is_case_insensitive(client, outbox):
    request_code(client, "  BEN@Example.com ")
    assert len(outbox) == 1


def test_wrong_code(client, outbox):
    request_code(client)
    response = enter_code(client, "000000" if code_from(outbox[0]) != "000000" else "111111")
    assert response.status_code == 401
    assert not logged_in_as(client)


def test_five_wrong_guesses_burn_the_code(client, outbox):
    request_code(client)
    code = code_from(outbox[0])
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    for _ in range(5):
        enter_code(client, wrong)
    assert enter_code(client, code).status_code == 401


def test_expired_code(app, client, outbox):
    request_code(client)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE login_tokens SET expires_at = '2000-01-01 00:00:00'")
        db.commit()
    assert enter_code(client, code_from(outbox[0])).status_code == 401


def test_new_code_replaces_the_old_one(client, outbox):
    request_code(client)
    request_code(client)
    first, second = (code_from(m) for m in outbox)
    if first != second:
        assert enter_code(client, first).status_code == 401
    assert enter_code(client, second).status_code == 302


def test_link_asks_for_confirmation_then_logs_in_once(app, outbox):
    client = app.test_client()
    request_code(client)
    link = link_from(outbox[0])

    other_browser = app.test_client()
    page = other_browser.get(link)
    assert page.status_code == 200 and "Log in as Ben Novak" in page.get_data(as_text=True)
    assert not logged_in_as(other_browser)  # opening the link alone does nothing

    token = link.split("token=")[1]
    response = other_browser.post("/login/link", data={"csrf": csrf_from(page), "token": token})
    assert response.status_code == 302
    assert logged_in_as(other_browser)

    assert app.test_client().get(link).headers["Location"] == "/login"  # used up
    assert enter_code(client, code_from(outbox[0])).status_code == 401  # and so is its code


def test_visiting_a_page_mid_login_keeps_the_pending_code(client, outbox):
    request_code(client)
    form = client.get("/login/code")
    client.get("/")  # e.g. the dashboard opened in another tab
    response = client.post("/login/code", data={"csrf": csrf_from(form), "code": code_from(outbox[0])})
    assert response.status_code == 302


def test_code_works_only_once(app, client, outbox):
    login(client, outbox)
    second = app.test_client()
    with second.session_transaction() as s:
        s["pending_login"] = {"token": 1, "email": BEN, "next": ""}
    assert enter_code(second, code_from(outbox[0])).status_code == 401


def test_emails_per_account_are_limited(client, outbox):
    for _ in range(6):
        request_code(client)
    assert len(outbox) == 5


def test_requests_per_ip_are_limited(client, outbox):
    for _ in range(10):
        request_code(client, "stranger@example.com")
    assert request_code(client).status_code == 429
    assert outbox == []


def test_wrong_codes_per_ip_lock_out(client, outbox):
    request_code(client, "stranger@example.com")
    for _ in range(10):
        enter_code(client, "123456")
    request_code(client)
    assert enter_code(client, code_from(outbox[0])).status_code == 429


def test_smtp_failure_is_reported_and_leaves_no_token(app, client, monkeypatch):
    def broken(message):
        raise OSError("connection refused")

    monkeypatch.setattr(mail, "deliver", broken)
    response = request_code(client)
    assert response.status_code == 503
    assert "could not be sent" in response.get_data(as_text=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM login_tokens").fetchone()[0] == 0


def test_session_ends_when_user_is_removed(app, client, outbox):
    login(client, outbox)
    with app.app_context():
        db = get_db()
        db.execute("DELETE FROM users WHERE email = ?", (BEN,))
        db.commit()
    assert client.get("/").status_code == 302


def test_logout(client, outbox):
    login(client, outbox)
    client.post("/logout", data={"csrf": csrf_from(client.get("/"))})
    assert not logged_in_as(client)


def test_deliver_uses_implicit_tls_on_465(app, monkeypatch):
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            calls["connect"] = (host, port, context, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, message):
            calls["message"] = message

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    with app.app_context():
        mail.deliver(mail.build("ben@example.com", "Hi", "Body"))
    host, port, context, _ = calls["connect"]
    assert (host, port) == ("mail.example", 465)
    assert isinstance(context, ssl.SSLContext) and context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert calls["login"] == ("info@example.com", "smtp-password")
    assert calls["message"]["Message-ID"].endswith("@example.com>")
