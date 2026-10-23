"""Outgoing email over SMTPS (implicit TLS, port 465). Every message goes
through deliver() so tests can replace that one function."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from flask import current_app, render_template


def deliver(message):
    cfg = current_app.config
    # Default context: verifies the server certificate and hostname.
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"], context=context, timeout=20) as smtp:
        smtp.login(cfg["SMTP_USERNAME"], cfg["SMTP_PASSWORD"])
        smtp.send_message(message)


def build(to, subject, text, html=None):
    sender = current_app.config["MAIL_FROM"]
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=parseaddr(sender)[1].rpartition("@")[2] or None)
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    return message


def send_login_email(user, code, link, minutes):
    context = {"name": user["name"].split()[0], "code": code, "link": link, "minutes": minutes,
               "site": current_app.config["PUBLIC_URL"].removeprefix("https://")}
    deliver(build(
        formataddr((user["name"], user["email"])),
        f"Finance login code: {code}",
        render_template("email/login.txt", **context),
        render_template("email/login.html", **context),
    ))
