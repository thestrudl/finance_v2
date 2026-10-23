import pytest

from finance.parser import parse


@pytest.mark.parametrize("text, kind, category, amount, comment", [
    ("Food 5.5 Jurman", "expense", "Food", "5.5", "Jurman"),
    ("food 5,5", "expense", "food", "5,5", ""),
    ("Bike -7.86 partial refund", "expense", "Bike", "-7.86", "partial refund"),
    ("Tečaj 600", "expense", "Tečaj", "600", ""),
    ("food 5 report", "expense", "food", "5", "report"),
    ("in placa 2500 october bonus", "income", "placa", "2500", "october bonus"),
    ("IN Salary 500 Company", "income", "Salary", "500", "Company"),
])
def test_entries(text, kind, category, amount, comment):
    parsed = parse(text)
    assert (parsed.kind, parsed.category, parsed.amount, parsed.comment) == (kind, category, amount, comment)
    assert parsed.is_income == (kind == "income")


@pytest.mark.parametrize("text, is_income, category, day, comment", [
    ("repeat tel 19.89 18 ana telefon", False, "tel", 18, "ana telefon"),
    ("REPEAT Food 5.5 10 comment", False, "Food", 10, "comment"),
    ("repeat in placa 2500 15", True, "placa", 15, ""),
    ("repeat in salary 1000 15 company", True, "salary", 15, "company"),
])
def test_repeats(text, is_income, category, day, comment):
    parsed = parse(text)
    assert parsed.kind == "repeat"
    assert (parsed.is_income, parsed.category, parsed.day, parsed.comment) == (is_income, category, day, comment)


@pytest.mark.parametrize("text", ["repeat food 5", "repeat food 5 comment", "repeat food 5 32 x", "repeat in placa 100"])
def test_repeat_needs_valid_day(text):
    assert parse(text).kind == "bad_day"


@pytest.mark.parametrize("text, command", [
    ("help", "help"), ("Help", "help"), ("/help", "help"), ("/help@finance_bot", "help"),
    ("categories", "categories"), ("report", "report"), ("summary", "summary"),
    ("ai report", "summary"), ("AI Report", "summary"),
    ("list repeat", "list_repeat"), ("version", "version"), ("undo", "undo"), ("dashboard", "dashboard"),
])
def test_commands(text, command):
    assert parse(text).kind == command


def test_income_without_category_is_not_an_expense_called_in():
    assert parse("in 50 lunch").kind == "in_word"


@pytest.mark.parametrize("text", ["hello", "food five", "5 food", "can you help me"])
def test_unknown(text):
    assert parse(text).kind == "unknown"
