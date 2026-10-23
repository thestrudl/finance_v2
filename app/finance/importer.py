"""One-time import of the Google Sheet.

Either the whole workbook as .ods (File > Download > OpenDocument), which
brings webHookData, repeatData, montlyBudget and Categories and is checked
against the yearly sheets; or single tabs as .csv (File > Download > CSV,
which saves only the open tab): webHookData, and optionally repeatData.
"""
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta

from . import categories
from .util import TS_FORMAT, month_bounds, now, to_cents, ts

_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
MAX_REPEAT = 64  # formatted-but-empty cells/rows repeat thousands of times
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _cell(cell):
    kind = cell.get(_OFFICE + "value-type")
    if kind in ("float", "currency", "percentage"):
        return float(cell.get(_OFFICE + "value"))
    if kind == "date":
        return datetime.fromisoformat(cell.get(_OFFICE + "date-value"))
    if kind == "boolean":
        return cell.get(_OFFICE + "boolean-value") == "true"
    paragraphs = ["".join(p.itertext()) for p in cell.findall(_TEXT + "p")]
    return "\n".join(paragraphs) if paragraphs else None


def read_ods(source):
    """{sheet name: rows of cell values}; `source` is a path or a binary file."""
    with zipfile.ZipFile(source) as archive:
        root = ET.fromstring(archive.read("content.xml"))
    sheets = {}
    for table in root.iter(_TABLE + "table"):
        rows = []
        for row in table.iter(_TABLE + "table-row"):
            values = []
            for cell in row:
                if cell.tag in (_TABLE + "table-cell", _TABLE + "covered-table-cell"):
                    repeat = int(cell.get(_TABLE + "number-columns-repeated", "1"))
                    values.extend([_cell(cell)] * min(repeat, MAX_REPEAT))
            while values and values[-1] is None:
                values.pop()
            repeat = int(row.get(_TABLE + "number-rows-repeated", "1"))
            rows.extend([values] * (min(repeat, MAX_REPEAT) if values else 1))
        while rows and not rows[-1]:
            rows.pop()
        sheets[table.get(_TABLE + "name")] = rows
    return sheets


def _sheet(sheets, name):
    # Apps Script's getSheetByName ignores case; the code says "webHookData",
    # the tab says "WebHookData".
    for sheet_name, rows in sheets.items():
        if sheet_name.lower() == name.lower():
            return rows
    return None


def _pad(row, width):
    return (list(row) + [None] * width)[:width]


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def import_transactions(db, rows, replace=False):
    """webHookData rows (no header): [when, user, price, category, comment, income]."""
    existing = db.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0]
    if existing and not replace:
        raise ValueError(f"{existing} imported transactions already exist. Use --replace to import again.")
    if replace:
        db.execute("DELETE FROM transactions WHERE source = 'import'")

    summary = Counter()
    stamp = ts(now())
    for row in rows:
        when, user, price, category, comment, income = _pad(row, 6)
        if not (isinstance(when, datetime) and isinstance(price, float)
                and isinstance(category, str) and category.strip()):
            # Rows holding only a date come from a cleared repeat row the old
            # trigger kept logging.
            if any(value not in (None, "") for value in row):
                summary["skipped rows"] += 1
            continue
        user = _text(user)
        db.execute(
            """INSERT INTO transactions (occurred_at, user_name, amount_cents, category, category_raw,
                   comment, is_income, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'import', ?)""",
            (when.strftime(TS_FORMAT), None if user in ("", "undefined undefined") else user,
             to_cents(price), categories.resolve(db, category), category.strip(), _text(comment),
             1 if income in (1.0, True) else 0, stamp),
        )
        summary["transactions"] += 1
    dropped = drop_reversed_pairs(db)
    if dropped:
        summary["transactions"] -= 2 * dropped
        summary["reversed pairs dropped"] = dropped
    return summary


def drop_reversed_pairs(db, minutes=30):
    """In the sheet, a wrong entry was fixed by logging its exact negative right
    after it ("food 5", then "food -5"). Each such pair adds up to zero, so both
    go: totals stay the same, the noise goes. Only imported rows are touched."""
    pairs = db.execute(
        """SELECT a.id, b.id FROM transactions a JOIN transactions b
             ON lower(trim(a.category_raw)) = lower(trim(b.category_raw)) AND a.is_income = b.is_income
            AND b.amount_cents = -a.amount_cents AND a.amount_cents > 0 AND b.id != a.id
            AND b.occurred_at >= a.occurred_at
            AND julianday(b.occurred_at) - julianday(a.occurred_at) <= ? / 1440.0
           WHERE a.source = 'import' AND b.source = 'import'
           ORDER BY a.occurred_at, b.occurred_at""",
        (minutes,),
    ).fetchall()
    used = set()
    for first, second in pairs:
        if first not in used and second not in used:
            used.update((first, second))
    db.executemany("DELETE FROM transactions WHERE id = ?", [(i,) for i in used])
    return len(used) // 2


def import_repeats(db, rows, today):
    """repeatData rows (no header): [created, user, price, category, comment, income, day]."""
    summary = Counter()
    existing = db.execute("SELECT COUNT(*) FROM repeats").fetchone()[0]
    # Repeats only go in once; after that they are managed in the portal.
    if existing:
        summary["repeats kept (already set up)"] = existing
        return summary
    # The sheet already logged everything due up to today.
    active_from = (today + timedelta(days=1)).isoformat()
    stamp = ts(now())
    for row in rows:
        _, user, price, category, comment, income, day = _pad(row, 7)
        if not (isinstance(price, float) and isinstance(category, str) and category.strip()
                and isinstance(day, float) and 1 <= day <= 31):
            continue
        db.execute(
            """INSERT INTO repeats (user_name, amount_cents, category, comment, is_income, day_of_month,
                   active_from, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_text(user) or None, to_cents(price), category.strip(), _text(comment),
             1 if income in (1.0, True) else 0, int(day), active_from, stamp),
        )
        summary["repeats"] += 1
    return summary


def import_workbook(db, sheets, today, replace=False):
    log_rows = _sheet(sheets, "webHookData")
    if log_rows is None:
        raise ValueError("The file has no webHookData sheet.")

    # The category lists from the Categories sheet, with their spelling.
    for row in (_sheet(sheets, "Categories") or [])[1:]:
        for cell in row[:2]:
            if isinstance(cell, str) and cell.strip():
                categories.resolve(db, cell)

    summary = import_transactions(db, log_rows[1:], replace)
    summary.update(import_repeats(db, (_sheet(sheets, "repeatData") or [])[1:], today))

    periods = {"monthly": "month", "yearly": "year"}
    for row in (_sheet(sheets, "montlyBudget") or [])[1:]:
        category, budget, period = _pad(row, 3)
        if isinstance(category, str) and isinstance(budget, float) and budget > 0                 and _text(period).lower() in periods:
            key = categories.resolve(db, category)
            cur = db.execute(
                "UPDATE categories SET budget_cents = ?, budget_period = ? WHERE key = ? AND budget_cents IS NULL",
                (to_cents(budget), periods[_text(period).lower()], key),
            )
            summary["budgets"] += cur.rowcount

    db.commit()
    return summary


def compare_with_year_sheets(db, sheets):
    """Months where the yearly sheets' Income/Expenses differ from the database.
    Returns (checked month count, list of mismatches)."""
    checked, mismatches = 0, []
    for name, rows in sheets.items():
        if not (name.isdigit() and len(name) == 4):
            continue
        for row in rows:
            row = _pad(row, 6)
            if row[2] not in MONTHS or not isinstance(row[3], float) or not isinstance(row[4], float):
                continue
            ym = f"{name}-{MONTHS.index(row[2]) + 1:02d}"
            start, end = month_bounds(ym)
            income, expense = db.execute(
                """SELECT COALESCE(SUM(CASE WHEN is_income THEN amount_cents END), 0),
                          COALESCE(SUM(CASE WHEN NOT is_income THEN amount_cents END), 0)
                   FROM transactions WHERE occurred_at >= ? AND occurred_at < ?""",
                (start, end),
            ).fetchone()
            checked += 1
            if abs(income - to_cents(row[3])) > 1 or abs(expense - to_cents(row[4])) > 1:
                mismatches.append((ym, to_cents(row[3]), income, to_cents(row[4]), expense))
            if row[2] == "December":
                break  # only the first month table is Income/Expenses
    return checked, mismatches


# ── CSV ─────────────────────────────────────────────────────────────────────
# A CSV holds what the sheet *displays*, so dates, amounts and TRUE/FALSE come
# in the spreadsheet's locale. Your sheet is en-US ("5/4/2024 9:09:12", "6.04",
# "FALSE"); day-first dates, decimal commas and ; delimiters work too.

_DATE = re.compile(
    r"^(\d{1,4})[./-] ?(\d{1,2})[./-] ?(\d{2,4})\.?"
    r"(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?(?: ?([AaPp][Mm]))?)?$"
)
_TRUE = {"true", "yes", "da", "pravilno", "wahr", "x"}
_FALSE = {"false", "no", "ne", "nepravilno", "falsch", ""}


def read_csv(data):
    """Rows of strings from CSV bytes; the delimiter is taken from the first line."""
    text = data.decode("utf-8-sig")
    first_line = text.split("\n", 1)[0]
    delimiter = max(",;\t", key=first_line.count)
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if any(cell.strip() for cell in row)]


def parse_amount(text):
    """'6.04', '6,04', '1,234.56', '1.234,56', '€ -7,86' -> float; None if not a number."""
    value = re.sub(r"[\s\u00a0€$]|EUR", "", text or "")
    if value.count(",") and value.count("."):
        decimal = "," if value.rfind(",") > value.rfind(".") else "."
        value = value.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif value.count(",") == 1:
        value = value.replace(",", ".")
    elif value.count(",") > 1 or value.count(".") > 1:
        value = value.replace(",", "").replace(".", "")
    try:
        return float(value)
    except ValueError:
        return None


def parse_flag(text):
    """The Income column: True / False, or None if it says something else."""
    value = (text or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    number = parse_amount(value)
    return None if number is None else number != 0


def parse_dates(texts, order=None):
    """Datetimes (None where unreadable) plus the date order used: 'ymd', 'mdy' or 'dmy'.

    For 5/4/2024 the order is worked out from the whole column: a first number
    above 12 means day-first, a second number above 12 means month-first."""
    parts = [_DATE.match((text or "").strip()) for text in texts]
    ambiguous = [m for m in parts if m and len(m[1]) <= 2]
    if order is None and ambiguous:
        day_first = any(int(m[1]) > 12 for m in ambiguous)
        month_first = any(int(m[2]) > 12 for m in ambiguous)
        if day_first and month_first:
            raise ValueError("The dates mix day-first and month-first; pass --date-order dmy or mdy.")
        if month_first:
            order = "mdy"
        elif day_first:
            order = "dmy"
        else:
            # Nothing above 12 anywhere: go by the separator the locale uses.
            order = "mdy" if "/" in ambiguous[0][0] else "dmy"

    result = []
    for m in parts:
        if not m:
            result.append(None)
            continue
        a, b, c, hour, minute, second, ampm = m.groups()
        if len(a) == 4:
            year, month, day = int(a), int(b), int(c)
        else:
            year = int(c) + (2000 if len(c) == 2 else 0)
            month, day = (int(a), int(b)) if order == "mdy" else (int(b), int(a))
        hour = int(hour or 0)
        if ampm:
            hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
        try:
            result.append(datetime(year, month, day, hour, int(minute or 0), int(second or 0)))
        except ValueError:
            result.append(None)
    return result, order or "ymd"


def _without_header(rows):
    return rows[1:] if rows and not _DATE.match(rows[0][0].strip() if rows[0] else "") else rows


def csv_transactions(rows, order=None):
    """webHookData CSV rows -> typed rows for import_transactions, and the date order."""
    body = _without_header(rows)
    dates, order = parse_dates([row[0] if row else "" for row in body], order)
    typed = []
    for when, row in zip(dates, body):
        _, user, price, category, comment, income = _pad(row, 6)
        flag = parse_flag(income)
        # An unreadable Income cell would silently turn income into an expense: skip the row instead.
        amount = parse_amount(price) if flag is not None else None
        typed.append([when, user, amount, category, comment, flag])
    return typed, order


def csv_repeats(rows):
    """repeatData CSV rows -> typed rows for import_repeats."""
    typed = []
    for row in _without_header(rows):
        _, user, price, category, comment, income, day = _pad(row, 7)
        typed.append([None, user, parse_amount(price), category, comment, parse_flag(income), parse_amount(day)])
    return typed


def imported_by_year(db):
    """What the import added, per year, to hold against the sheet's yearly totals."""
    return db.execute(
        """SELECT substr(occurred_at, 1, 4) AS year, COUNT(*) AS n, MIN(occurred_at) AS first, MAX(occurred_at) AS last,
               COALESCE(SUM(CASE WHEN is_income THEN amount_cents END), 0) AS income,
               COALESCE(SUM(CASE WHEN NOT is_income THEN amount_cents END), 0) AS expense
           FROM transactions WHERE source = 'import' GROUP BY year ORDER BY year"""
    ).fetchall()
