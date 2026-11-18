"""A bank statement checked against what was logged: which payments never
made it into the bot.

Reads the NLB "promet" CSV export (semicolon-separated, cp1250, decimal
comma). Each payment is matched to a logged entry of the same amount within
MATCH_DAYS of it; a rounded amount logged within CLOSE_DAYS counts as a close
match, shown for review. Amounts that add up count too: one entry paid in
several payments (a 1000 deposit paid as 2 × 500), or one payment for several
entries (an order logged item by item). Rules in household.json say how lines are
read:

    "statement": [
      {"match": "ANA NOVAK", "skip": "own transfer"},
      {"match": "CITY POWER", "category": "elektrika", "comment": "power bill"}
    ]

`match` is looked for, ignoring case, in the payee and the purpose. A skip
rule leaves the line out (own transfers between accounts), a category rule
suggests where a missing line belongs.
"""
import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations

MATCH_DAYS = 7  # an exact amount logged this many days before or after the payment
CLOSE_DAYS = 2  # a rounded amount must be logged closer to it
COMBINE_DAYS = 3  # entries that add up to one payment are logged around it
MAX_PARTS = 3  # at most this many payments or entries add up to one another
MIN_SUM_CENTS = 20_00  # small amounts add up to each other by chance, so they aren't summed
ONE_ORDER_MINUTES = timedelta(minutes=15)  # entries logged this close together are one purchase
SUBSET_LIMIT = 30  # candidates tried for a sum, nearest first, to keep it quick
GENERIC_PURPOSES = {"PLAČILO", "PLACILO", "TAKOJŠNJE PLAČILO - ODLIV", "POS TERMINAL"}


@dataclass
class Line:
    day: date
    cents: int  # money out is positive, like an expense; money in is negative
    payee: str
    purpose: str
    rule: dict = field(default_factory=dict)

    @property
    def is_income(self):
        return self.cents < 0

    @property
    def amount(self):
        return abs(self.cents)

    @property
    def text(self):
        """Payee and purpose, without repeating one in the other."""
        if not self.purpose or self.purpose.upper() in GENERIC_PURPOSES or self.purpose == self.payee:
            return self.payee or self.purpose
        if not self.payee or self.payee.upper() == "POS TERMINAL":
            return self.purpose
        return f"{self.payee} · {self.purpose}"


def _cents(text):
    text = (text or "").strip().replace(".", "").replace(",", ".")
    return round(float(text) * 100) if text else 0


def _date(text):
    return datetime.strptime(text.strip(), "%d.%m.%Y").date()


def read_nlb(data):
    """Statement lines from the NLB CSV export, oldest first."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1250")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = [name.strip().upper() for name in next(reader)]

    def column(*names):
        for name in names:
            if name in header:
                return header.index(name)
        raise ValueError(f"Not an NLB statement: no {names[0]} column")

    posted, value = column("DATUM KNJIŽENJA"), column("DATUM VALUTE")
    credit, debit = column("DOBRO"), column("BREME")
    purpose, payee = column("NAMEN"), column("UDELEŽENEC - NAZIV")
    lines = []
    for row in reader:
        if len(row) <= max(posted, value, credit, debit, purpose, payee):
            continue
        cents = _cents(row[debit]) - _cents(row[credit])
        if not cents:
            continue
        # Card payments are booked a day or two late; the value date is the purchase.
        day = _date(row[value] or row[posted])
        lines.append(Line(day, cents, row[payee].strip(), row[purpose].strip()))
    lines.sort(key=lambda line: line.day)
    return lines


def apply_rules(lines, rules):
    for line in lines:
        haystack = f"{line.payee} {line.purpose}".upper()
        line.rule = next((rule for rule in rules if rule.get("match", "").upper() in haystack), {})
    return lines


@dataclass
class Result:
    line: Line
    # "logged", "close" (amount a little off), "split" (one entry paid in several
    # payments), "combined" (one payment for several entries), "rounded_sum" (the
    # same, a little off: for review), "reversed" (logged with the wrong sign),
    # "skipped" or "missing"
    status: str
    entries: list = field(default_factory=list)

    @property
    def entry(self):
        return self.entries[0] if self.entries else None


def _exact(line, entry):
    return entry["amount_cents"] == line.amount and abs((entry["day"] - line.day).days) <= MATCH_DAYS


def _close(line, entry):
    """A rounded amount ("249" for 249.83), logged within CLOSE_DAYS, and in
    the line's category when a rule names one."""
    a, b = entry["amount_cents"], line.amount
    category = line.rule.get("category")
    return (abs(a - b) <= max(100, round(0.02 * max(a, b))) and abs((entry["day"] - line.day).days) <= CLOSE_DAYS
            and (not category or entry["category"] == category))


def compare(db, lines):
    """Each statement line with the logged entry it matches, if any. Every
    entry matches at most one line; exact amounts are paired first."""
    if not lines:
        return []
    start = datetime.combine(lines[0].day - timedelta(days=MATCH_DAYS), datetime.min.time())
    end = datetime.combine(lines[-1].day + timedelta(days=MATCH_DAYS + 1), datetime.min.time())
    entries = [dict(row) for row in db.execute(
        """SELECT t.id, t.occurred_at, t.amount_cents, t.category, t.comment, t.is_income, c.label
           FROM transactions t JOIN categories c ON c.key = t.category
           WHERE t.occurred_at >= ? AND t.occurred_at < ?""",
        (start.isoformat(" "), end.isoformat(" ")),
    )]
    for entry in entries:
        entry["day"] = date.fromisoformat(entry["occurred_at"][:10])

    results = {id(line): Result(line, "skipped" if line.rule.get("skip") else "missing") for line in lines}
    used = set()
    # Exact pairings first, then sums, then the looser ones.
    _match_single(results, lines, entries, used, "logged", _exact)
    _match_split(results, lines, entries, used, rounded=False)
    _match_combined(results, lines, entries, used, rounded=False)
    _match_split(results, lines, entries, used, rounded=True)
    _match_single(results, lines, entries, used, "close", _close)
    _match_combined(results, lines, entries, used, rounded=True)
    _match_reversed(results, lines, entries, used)
    return [results[id(line)] for line in lines]


def _match_single(results, lines, entries, used, status, fits):
    for line in lines:
        result = results[id(line)]
        if result.status != "missing":
            continue
        candidates = [e for e in entries
                      if e["id"] not in used and bool(e["is_income"]) == line.is_income and fits(line, e)]
        if candidates:
            best = min(candidates, key=lambda e: (abs(e["amount_cents"] - line.amount),
                                                  abs((e["day"] - line.day).days)))
            used.add(best["id"])
            result.status, result.entries = status, [best]


def _match_reversed(results, lines, entries, used):
    """Logged with the wrong sign: money went out, but "-70" was logged (a
    refund), or the other way round. Fix the entry rather than add another."""
    for line in lines:
        result = results[id(line)]
        if result.status != "missing":
            continue
        for entry in entries:
            if (entry["id"] not in used and bool(entry["is_income"]) == line.is_income
                    and entry["amount_cents"] == -line.amount and abs((entry["day"] - line.day).days) <= MATCH_DAYS):
                used.add(entry["id"])
                result.status, result.entries = "reversed", [entry]
                break


def _subset(items, target, amount, tolerance=0, together=lambda combo: True):
    """The first 2..MAX_PARTS items whose amounts add up to `target` (within
    `tolerance` cents) and that `together` accepts, or None."""
    for size in range(2, MAX_PARTS + 1):
        for combo in combinations(items, size):
            if abs(sum(amount(item) for item in combo) - target) <= tolerance and together(combo):
                return combo
    return None


def _payee_key(line):
    # "BAUHAUS BLAGAJNA 10" and "BAUHAUS d.o.o., k.d. 9" are the same shop.
    words = line.payee.upper().split()
    return words[0] if words else ""


def _match_split(results, lines, entries, used, rounded):
    """One logged entry paid in several bank payments to the same payee:
    "hisa 1000 kuhinja" logged once, paid as 2 × 500. With `rounded`, a
    total rounded when logged (148.69 + 6.59 logged as 155) within CLOSE_DAYS."""
    days, tolerance = (CLOSE_DAYS, 100) if rounded else (MATCH_DAYS, 0)
    for entry in sorted(entries, key=lambda e: -e["amount_cents"]):
        if entry["id"] in used or entry["amount_cents"] < MIN_SUM_CENTS:
            continue
        open_lines = [line for line in lines
                      if results[id(line)].status == "missing" and line.is_income == bool(entry["is_income"])
                      and line.amount < entry["amount_cents"] and abs((line.day - entry["day"]).days) <= days
                      and line.rule.get("category") in (None, entry["category"])]
        by_payee = {}
        for line in open_lines:
            by_payee.setdefault(_payee_key(line), []).append(line)
        for group in by_payee.values():
            parts = _subset(group[:SUBSET_LIMIT], entry["amount_cents"], lambda line: line.amount, tolerance)
            if parts:
                used.add(entry["id"])
                for line in parts:
                    results[id(line)].status, results[id(line)].entries = "split", [entry]
                break


def _match_combined(results, lines, entries, used, rounded):
    """One bank payment covering several logged entries, e.g. an order logged
    item by item, within COMBINE_DAYS of the payment. The entries must look
    like one purchase: in one category and logged within a day of each other,
    or in any categories but logged minutes apart (one Amazon order split into
    "hisa 130" and "app 87"). Anything looser adds up by chance too often.

    With `rounded`, entries logged minutes apart may add up to within 1 € of
    the payment (130 + 87 for 217.40). Entries logged in a row while catching
    up add up to almost anything that roughly, so these are for review."""
    status, tolerance = ("rounded_sum", 100) if rounded else ("combined", 0)
    for line in lines:
        result = results[id(line)]
        if result.status != "missing" or line.amount < MIN_SUM_CENTS:
            continue
        near = [e for e in entries
                if e["id"] not in used and bool(e["is_income"]) == line.is_income
                and 0 < e["amount_cents"] < line.amount and abs((e["day"] - line.day).days) <= COMBINE_DAYS
                and line.rule.get("category") in (None, e["category"])]
        near.sort(key=lambda e: abs((e["day"] - line.day).days))
        parts = _subset(near[:SUBSET_LIMIT], line.amount, lambda e: e["amount_cents"], tolerance,
                        together=lambda combo: _one_purchase(combo, exact=not rounded))
        if parts:
            used.update(e["id"] for e in parts)
            result.status, result.entries = status, list(parts)


def _one_purchase(combo, exact):
    """Logged minutes apart, or (for an exact total) in one category within a day."""
    times = [datetime.fromisoformat(e["occurred_at"]) for e in combo]
    if max(times) - min(times) <= ONE_ORDER_MINUTES:
        return True
    return exact and len({e["category"] for e in combo}) == 1 and (max(times).date() - min(times).date()).days <= 1


def missing_csv(results):
    """The missing lines as CSV in the bot's import format (date time, user,
    price, category, comment, income), for `flask import-csv --append` once
    the categories are filled in."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date time", "User", "Price", "Category", "Comment", "Income"])
    for result in results:
        if result.status != "missing":
            continue
        line = result.line
        writer.writerow([f"{line.day.isoformat()} 12:00:00", "", f"{line.amount / 100:.2f}",
                         line.rule.get("category", ""), line.rule.get("comment") or line.text,
                         "TRUE" if line.is_income else "FALSE"])
    return out.getvalue()
