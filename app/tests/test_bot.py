from datetime import timedelta

from conftest import ADMIN, GROUP_CHAT, add_tx, callback_update, message_update, post_update, texts

from finance import bot
from finance.db import get_db
from finance.util import fmt_month, month_key, now, shift_month


def rows(app, sql="SELECT * FROM transactions ORDER BY id"):
    with app.app_context():
        return [dict(r) for r in get_db().execute(sql).fetchall()]


def test_wrong_path_is_404(client, sent):
    assert post_update(client, message_update("food 5"), path="guess").status_code == 404


def test_wrong_or_missing_secret_header_is_403(client, sent):
    assert post_update(client, message_update("food 5"), secret="nope").status_code == 403
    response = client.post("/telegram/hook-path", json=message_update("food 5"))
    assert response.status_code == 403


def test_expense_is_stored_with_sender_name(app, client, sent):
    assert post_update(client, message_update("Food 5,5 Jurman")).status_code == 200
    [tx] = rows(app)
    assert tx["amount_cents"] == 550
    assert (tx["category"], tx["category_raw"], tx["comment"]) == ("food", "Food", "Jurman")
    assert (tx["user_name"], tx["telegram_user_id"], tx["is_income"], tx["source"]) == ("Ana Novak", 5, 0, "telegram")
    assert texts(sent, GROUP_CHAT) == ["Expense stored (Food, 5,5, Jurman) Thanks Ana Novak"]


def test_income_is_stored(app, client, sent):
    post_update(client, message_update("in placa 2500 october", first="Ben", last=None))
    [tx] = rows(app)
    assert (tx["is_income"], tx["amount_cents"], tx["category"]) == (1, 250000, "placa")
    assert texts(sent) == ["Income stored (placa, 2500, october) Thanks Ben"]


def test_misspelling_lands_in_the_aliased_category(app, client, sent):
    post_update(client, message_update("Clotes 20 jeans"))
    assert rows(app)[0]["category"] == "clothing"


def test_redelivered_update_is_stored_once(app, client, sent):
    update = message_update("food 5")
    post_update(client, update)
    post_update(client, update)
    assert len(rows(app)) == 1


def test_unknown_chat_is_refused_and_admin_told(app, client, sent):
    post_update(client, message_update("food 5", chat_id=-999, user_id=77))
    assert rows(app) == []
    assert "not set up" in texts(sent, -999)[0]
    assert "-999" in texts(sent, str(ADMIN))[0]


def test_admin_can_use_the_bot_from_any_chat(app, client, sent):
    post_update(client, message_update("food 5", chat_id=ADMIN, user_id=ADMIN))
    assert len(rows(app)) == 1


def test_wrong_format_reply(client, sent):
    post_update(client, message_update("hello there"))
    assert texts(sent) == ['Wrong format. Write "help" if you need help ;)']


def test_non_text_message_is_ignored(client, sent):
    update = message_update("x")
    del update["message"]["text"]
    assert post_update(client, update).status_code == 200
    assert sent == []


def test_repeat_starts_tomorrow(app, client, sent):
    post_update(client, message_update("repeat tel 19.89 18 phone"))
    [rep] = rows(app, "SELECT * FROM repeats")
    with app.app_context():
        tomorrow = (now().date() + timedelta(days=1)).isoformat()
    assert (rep["amount_cents"], rep["day_of_month"], rep["active_from"]) == (1989, 18, tomorrow)
    assert texts(sent) == ["Repeat expense stored (tel, 19.89, day 18) Thanks Ana Novak"]


def test_undo_removes_only_your_own_last_entry(app, client, sent):
    post_update(client, message_update("food 5", user_id=5))
    post_update(client, message_update("gas 40", user_id=6, first="Ben"))
    post_update(client, message_update("drinks 3 kava", user_id=5))
    post_update(client, message_update("undo", user_id=5))
    assert [r["category"] for r in rows(app)] == ["food", "gas"]
    assert texts(sent)[-1] == "Removed expense: Drinks 3.00 (kava)"


def test_help_offers_buttons_and_dashboard_link(client, sent):
    post_update(client, message_update("help"))
    [(_, payload, _)] = sent
    buttons = [b for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert {"text": "Summary", "callback_data": "summaryMenu"} in buttons
    assert {"text": "Open dashboard", "url": "https://finance.example"} in buttons


def test_report_callback_sends_table_and_chart(app, client, sent):
    post_update(client, message_update("food 12.5"))
    post_update(client, message_update("gas 40"))
    sent.clear()
    post_update(client, callback_update("report"))
    methods = [m for m, _, _ in sent]
    assert methods[0] == "answerCallbackQuery"
    assert "sendPhoto" in methods
    assert texts(sent)[0].startswith("Gas\t40.00\nFood\t12.50")
    photo = next(files for m, _, files in sent if m == "sendPhoto")
    assert photo["photo"][1][:8] == b"\x89PNG\r\n\x1a\n"


def test_summary_command_offers_both_periods(client, sent):
    post_update(client, message_update("summary"))
    [(_, payload, _)] = sent
    data = [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert data == ["summaryWeekly", "summaryMonthly"]


def test_weekly_summary(client, sent):
    post_update(client, message_update("food 12.5 spar"))
    post_update(client, message_update("gas 61.20 petrol"))
    sent.clear()
    post_update(client, callback_update("summaryWeekly"))
    text = texts(sent)[-1]
    assert text.startswith("Summary · last 7 days")
    assert "Spending €73.70" in text
    assert "- Gas €61.20" in text and "petrol" in text


def test_monthly_summary_covers_last_month(app, client, sent):
    with app.app_context():
        last_month = shift_month(month_key(now()), -1)
    add_tx(app, f"{last_month}-03 10:00:00", 300000, "placa", is_income=True)
    add_tx(app, f"{last_month}-04 10:00:00", 4550, "food", comment="spar")
    post_update(client, callback_update("summaryMonthly"))
    text = texts(sent)[-1]
    assert text.startswith(f"Summary · {fmt_month(last_month)}")
    assert "Income €3,000.00" in text and "Spending €45.50" in text and "98% kept" in text
    assert f"Dashboard: https://finance.example/?month={last_month}" in text


def test_old_ai_report_buttons_give_the_summary(client, sent):
    post_update(client, message_update("food 5"))
    post_update(client, callback_update("aiWeekly"))
    assert texts(sent)[-1].startswith("Summary · last 7 days")


def test_handler_crash_still_answers_200_and_alerts_admin(client, sent, monkeypatch):
    def boom(*_):
        raise ValueError("kaput")

    monkeypatch.setattr(bot, "_store_entry", boom)
    assert post_update(client, message_update("food 5")).status_code == 200
    assert "kaput" in texts(sent, str(ADMIN))[0]
