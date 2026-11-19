from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import current_app

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def now():
    tz = ZoneInfo(current_app.config["APP_TIMEZONE"])
    return datetime.now(tz).replace(tzinfo=None, microsecond=0)


def today():
    return now().date()


def ts(moment):
    return moment.strftime(TS_FORMAT)


def parse_cents(value):
    """'1.500,50', '1500.5', '1 500' or '' -> cents (None for empty). Raises ValueError on junk."""
    text = (value or "").strip().replace(" ", "").replace("€", "")
    if not text:
        return None
    if "," in text and "." in text:
        # The later separator is the decimal one: 1.500,50 or 1,500.50.
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        amount = Decimal(text) * 100
    except InvalidOperation:
        raise ValueError(value) from None
    if not amount.is_finite():
        raise ValueError(value)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_day(value):
    """'YYYY-MM-DD' -> date, '' -> None. Raises ValueError on junk."""
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def plain_amount(cents):
    return "" if cents is None else f"{cents / 100:.2f}"


def fmt_eur(cents, sign=False):
    if cents is None:
        return "–"
    prefix = "−" if cents < 0 else ("+" if sign and cents > 0 else "")
    whole = abs(cents) % 100 == 0
    return f"{prefix}€{abs(cents) / 100:,.{0 if whole else 2}f}"


def fmt_day(value, year=None):
    """date or 'YYYY-MM-DD' -> '3 Feb' (with the year when it isn't `year`, default this year)."""
    if not value:
        return "–"
    day = date.fromisoformat(value) if isinstance(value, str) else value
    shown_year = year if year is not None else today().year
    return f"{day.day} {day:%b}" + ("" if day.year == shown_year else f" {day.year}")
