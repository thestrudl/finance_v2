"""Thin Telegram Bot API client. Every call goes through api() so tests can
replace that one function."""
import logging

import requests
from flask import current_app

log = logging.getLogger(__name__)

MAX_MESSAGE = 4000


def api(method, payload=None, files=None):
    url = f"https://api.telegram.org/bot{current_app.config['TELEGRAM_BOT_TOKEN']}/{method}"
    try:
        if files:
            response = requests.post(url, data=payload, files=files, timeout=60)
        else:
            response = requests.post(url, json=payload or {}, timeout=20)
        body = response.json()
    except (requests.RequestException, ValueError):
        log.exception("Telegram %s failed", method)
        return None
    if not body.get("ok"):
        log.warning("Telegram %s refused: %s", method, body.get("description"))
    return body


def send_text(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("sendMessage", payload)


def send_long_text(chat_id, text):
    """Split on line breaks so no message passes Telegram's length limit."""
    chunk = ""
    for line in text.split("\n"):
        while len(line) > MAX_MESSAGE:
            if chunk:
                send_text(chat_id, chunk)
                chunk = ""
            send_text(chat_id, line[:MAX_MESSAGE])
            line = line[MAX_MESSAGE:]
        if chunk and len(chunk) + len(line) + 1 > MAX_MESSAGE:
            send_text(chat_id, chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        send_text(chat_id, chunk)


def send_photo(chat_id, png, caption=""):
    return api(
        "sendPhoto",
        {"chat_id": str(chat_id), "caption": caption},
        files={"photo": ("chart.png", png, "image/png")},
    )


def edit_text(chat_id, message_id, text):
    """Replace a sent message's text; also removes its buttons."""
    return api("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})


def answer_callback(callback_id):
    return api("answerCallbackQuery", {"callback_query_id": callback_id})


def notify_admin(text):
    admin = current_app.config["TELEGRAM_ADMIN_ID"]
    if admin:
        send_long_text(admin, text)
