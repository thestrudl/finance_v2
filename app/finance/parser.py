"""Telegram message grammar, unchanged from the Apps Script bot:

    food 5.5 jurman               expense (negative amount = refund)
    in salary 2500 october        income
    repeat tel 19.89 18 phone     monthly expense, logged on day 18
    repeat in salary 2500 15      monthly income
    help | categories | report | summary | list repeat | version | undo | dashboard
    ("ai report" still works and means "summary")
    balance                       estimated bank & cash now
    balance 4520.30               what is really in the accounts and cash right now

Two deliberate differences: a command only triggers when it is the whole
message (so "food 5 report" is logged, not treated as "report"), and a decimal
comma works ("food 5,5").
"""
import re
from dataclasses import dataclass

AMOUNT = r"(\d+(?:[.,]\d+)?)"
SIGNED_AMOUNT = r"(-?\d+(?:[.,]\d+)?)"


def _command(words):
    # Optional leading slash and @botname, as Telegram adds them in groups.
    return re.compile(rf"^/?{words}(?:@\w+)?$", re.IGNORECASE)


COMMANDS = [
    ("help", _command(r"help")),
    ("categories", _command(r"categories")),
    ("summary", _command(r"(?:summary|ai\s*report)")),
    ("report", _command(r"report")),
    ("list_repeat", _command(r"list\s*repeat")),
    ("version", _command(r"version")),
    ("undo", _command(r"undo")),
    ("dashboard", _command(r"dashboard")),
]

# "balance" asks for the estimated bank & cash; "balance 4520.30" records what is really there.
BALANCE = re.compile(rf"^/?balance(?:@\w+)?(?:\s+{SIGNED_AMOUNT})?$", re.IGNORECASE)

INCOME = re.compile(rf"^in\s+(\w+)\s+{AMOUNT}(?:\s+(.*))?$", re.IGNORECASE)
REPEAT_IN = re.compile(rf"^repeat\s+in\s+(\w+)\s+{AMOUNT}(?:\s+(\d{{1,2}})(?=\s|$))?\s*(.*)$", re.IGNORECASE)
REPEAT_EX = re.compile(rf"^repeat\s+(\w+)\s+{AMOUNT}(?:\s+(\d{{1,2}})(?=\s|$))?\s*(.*)$", re.IGNORECASE)
EXPENSE = re.compile(rf"^(\w+)\s+{SIGNED_AMOUNT}(?:\s+(.*))?$")


@dataclass
class Parsed:
    kind: str  # a command name, "income", "expense", "repeat", "bad_day", "in_word" or "unknown"
    category: str = ""
    amount: str = ""  # as typed, echoed back in the reply
    comment: str = ""
    is_income: bool = False
    day: int = 0


def parse(text):
    text = text.strip()
    for name, pattern in COMMANDS:
        if pattern.match(text):
            return Parsed(name)

    if m := BALANCE.match(text):
        return Parsed("balance", amount=m[1] or "")

    if m := INCOME.match(text):
        return Parsed("income", m[1], m[2], (m[3] or "").strip(), is_income=True)

    for pattern, is_income in ((REPEAT_IN, True), (REPEAT_EX, False)):
        if m := pattern.match(text):
            day = int(m[3]) if m[3] else 0
            if not 1 <= day <= 31:
                return Parsed("bad_day", is_income=is_income)
            if m[1].lower() == "in":
                return Parsed("in_word")
            return Parsed("repeat", m[1], m[2], m[4].strip(), is_income=is_income, day=day)

    if m := EXPENSE.match(text):
        # "in 50 x" (category missing) would otherwise log an expense called "in".
        if m[1].lower() == "in":
            return Parsed("in_word")
        return Parsed("expense", m[1], m[2], (m[3] or "").strip())

    return Parsed("unknown")
