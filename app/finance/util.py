from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from flask import current_app

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def now():
    """Local wall-clock time, the same clock the spreadsheet used."""
    tz = ZoneInfo(current_app.config["APP_TIMEZONE"])
    return datetime.now(tz).replace(tzinfo=None, microsecond=0)


def ts(moment):
    return moment.strftime(TS_FORMAT)


def to_cents(value):
    """'5.5', '5,50' or 5.5 -> 550. Raises decimal.InvalidOperation on junk."""
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
    amount = Decimal(str(value)) * 100
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def plain_amount(cents):
    return f"{cents / 100:.2f}"


def fmt_eur(cents, sign=False):
    if cents is None:
        return "–"
    prefix = "−" if cents < 0 else ("+" if sign and cents > 0 else "")
    return f"{prefix}€{abs(cents) / 100:,.2f}"


def month_key(day):
    return f"{day.year:04d}-{day.month:02d}"


def shift_month(ym, months):
    year, month = (int(part) for part in ym.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_bounds(ym):
    """'2026-10' -> ('2026-10-01 00:00:00', '2026-11-01 00:00:00')."""
    return f"{ym}-01 00:00:00", f"{shift_month(ym, 1)}-01 00:00:00"


def fmt_month(ym, short=False):
    year, month = (int(part) for part in ym.split("-"))
    return date(year, month, 1).strftime("%b %Y" if short else "%B %Y")


def valid_month(value):
    try:
        datetime.strptime(value or "", "%Y-%m")
    except ValueError:
        return False
    return True
