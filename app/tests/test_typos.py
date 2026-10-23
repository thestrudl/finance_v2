import pytest
from conftest import add_tx, callback_update, message_update, post_update, texts

from finance import categories
from finance.db import get_db


@pytest.mark.parametrize("a, b, distance", [
    ("fodo", "food", 1),       # two letters swapped
    ("fod", "food", 1),        # one missing
    ("clothg", "clothing", 2),  # two missing
    ("insurnace", "insurance", 1),
    ("food", "food", 0),
    ("kitten", "sitting", 3),
])
def test_edit_distance(a, b, distance):
    assert categories.edit_distance(a, b) == distance


@pytest.fixture
def regulars(app):
    """Categories in regular use: food, clothing, gas (3+ entries each)."""
    for i in range(3):
        add_tx(app, f"2026-01-0{i + 1} 10:00:00", 1000, "food")
        add_tx(app, f"2026-01-0{i + 1} 11:00:00", 5000, "clothing")
        add_tx(app, f"2026-01-0{i + 1} 12:00:00", 4000, "gas")
    add_tx(app, "2026-01-05 10:00:00", 2000, "foto")  # used once: never offered


def suggest(app, typed):
    with app.app_context():
        return categories.suggest(get_db(), typed)


def test_suggestions(app, regulars):
    assert suggest(app, "Fodo") == "food"
    assert suggest(app, "clothng") == "clothing"
    assert suggest(app, "clothg") == "clothing"
    assert suggest(app, "foood") == "food"
    assert suggest(app, "food") is None       # known category
    assert suggest(app, "foto") is None       # known, even if rarely used
    assert suggest(app, "clotes") is None     # known alias of clothing
    assert suggest(app, "kayaking") is None   # nothing close
    assert suggest(app, "fodd") == "food"     # 1 off food; foto is not offered


def transactions(app):
    with app.app_context():
        return [dict(r) for r in get_db().execute("SELECT * FROM transactions WHERE source = 'telegram'")]


def question(sent):
    method, payload, _ = sent[-1]
    assert method == "sendMessage"
    buttons = [b for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    return payload["text"], {b["text"]: b["callback_data"] for b in buttons}


def test_typo_is_asked_about_and_nothing_stored_yet(app, client, sent, regulars):
    post_update(client, message_update("fodo 5.5 jurman"))
    text, buttons = question(sent)
    assert text == '"fodo" is not a category yet. Did you mean Food?\n\nfodo 5.5 jurman'
    assert list(buttons) == ["✓ Food", 'Keep "fodo"', "Cancel"]
    assert transactions(app) == []


def test_yes_stores_under_the_suggestion_and_learns_the_spelling(app, client, sent, regulars):
    post_update(client, message_update("fodo 5.5 jurman"))
    _, buttons = question(sent)
    post_update(client, callback_update(buttons["✓ Food"]))
    [tx] = transactions(app)
    assert (tx["category"], tx["category_raw"], tx["amount_cents"], tx["telegram_user_id"]) == ("food", "fodo", 550, 5)
    assert texts(sent)[-1] == "Expense stored (Food, 5.5, jurman) Thanks Ana Novak"

    sent.clear()
    post_update(client, message_update("fodo 3"))  # no question the second time
    assert texts(sent) == ["Expense stored (fodo, 3, ) Thanks Ana Novak"]
    assert transactions(app)[-1]["category"] == "food"


def test_keep_creates_the_new_category(app, client, sent, regulars):
    post_update(client, message_update("in fodo 100 market"))
    _, buttons = question(sent)
    post_update(client, callback_update(buttons['Keep "fodo"']))
    [tx] = transactions(app)
    assert (tx["category"], tx["is_income"]) == ("fodo", 1)
    assert texts(sent)[-1] == "Income stored (fodo, 100, market) Thanks Ana Novak"
    sent.clear()
    post_update(client, message_update("fodo 1"))
    assert texts(sent)[0].startswith("Expense stored")


def test_cancel_stores_nothing(app, client, sent, regulars):
    post_update(client, message_update("fodo 5"))
    _, buttons = question(sent)
    post_update(client, callback_update(buttons["Cancel"]))
    assert transactions(app) == []
    assert texts(sent)[-1] == "Cancelled, nothing stored."


def test_double_tap_stores_once(app, client, sent, regulars):
    post_update(client, message_update("fodo 5"))
    _, buttons = question(sent)
    post_update(client, callback_update(buttons["✓ Food"]))
    post_update(client, callback_update(buttons["✓ Food"]))
    assert len(transactions(app)) == 1


def test_old_question_expires(app, client, sent, regulars):
    post_update(client, message_update("fodo 5"))
    _, buttons = question(sent)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE pending_entries SET created_at = '2000-01-01 00:00:00'")
        db.commit()
    post_update(client, callback_update(buttons["✓ Food"]))
    assert transactions(app) == []
    assert "not stored" in texts(sent)[-1]


def test_answer_edits_the_question_when_telegram_gave_its_id(app, client, sent, monkeypatch, regulars):
    from finance import telegram

    calls = []

    def fake_api(method, payload=None, files=None):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 77}}

    monkeypatch.setattr(telegram, "api", fake_api)
    post_update(client, message_update("fodo 5"))
    data = calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    post_update(client, callback_update(data))
    method, payload = calls[-1]
    assert method == "editMessageText" and payload["message_id"] == 77
    assert payload["text"].startswith("Expense stored (Food")


def test_repeat_with_typo(app, client, sent, regulars):
    post_update(client, message_update("repeat clothng 20 5 jeans"))
    text, buttons = question(sent)
    assert "Did you mean Clothing?" in text and "repeat clothng 20 day 5" in text
    post_update(client, callback_update(buttons["✓ Clothing"]))
    with app.app_context():
        rep = get_db().execute("SELECT * FROM repeats").fetchone()
    assert (rep["category"], rep["amount_cents"], rep["day_of_month"]) == ("clothing", 2000, 5)
    assert texts(sent)[-1] == "Repeat expense stored (Clothing, 20, day 5) Thanks Ana Novak"


def test_known_and_new_words_are_stored_straight_away(app, client, sent, regulars):
    post_update(client, message_update("food 2"))
    post_update(client, message_update("kayaking 30"))
    assert len(transactions(app)) == 2
    assert all(t.startswith("Expense stored") for t in texts(sent))
