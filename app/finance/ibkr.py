"""Interactive Brokers holdings from the Flex Web Service (read-only).

A Flex Query set up once in Client Portal (Activity Flex, XML) is generated
on request: /SendRequest returns a reference code, /GetStatement returns the
report when it is ready. Sections used:

    Account Information          base currency
    Net Asset Value (NAV) in Base one row per day: cash, stocks, funds, ... total
    Open Positions               quantity, price, value, cost basis, unrealized P/L
    Cash Report                  cash per currency (ending, settled)
    Conversion Rates (optional)  to show foreign cash in the base currency

Values are as of the last close. Margin figures (excess liquidity, buying
power) are not part of Flex reports.
"""
import logging
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import current_app

from . import telegram
from .db import get_db
from .util import now, to_cents, ts

log = logging.getLogger(__name__)

BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
# "Try again shortly" codes from IBKR's error list; anything else is final.
RETRY_CODES = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1019", "1021"}
SYNC_AFTER_HOUR = 7          # local time; last business day's report is ready by then
RETRY_EVERY = timedelta(hours=1)


class FlexError(Exception):
    def __init__(self, code, message):
        super().__init__(f"IBKR Flex error {code}: {message}")
        self.code = code
        self.message = message


def _xml(response):
    body = response.content.lstrip()
    if not body.startswith(b"<"):
        raise FlexError("format", "the Flex Query must deliver XML, not CSV or text")
    return ET.fromstring(body)


def fetch_report(token, query_id, days=None, attempts=12, wait=5, http=requests, sleep=time.sleep):
    """The generated report as an XML element. `days` asks for that many days
    back (at most 365) instead of the query's own period."""
    params = {"t": token, "q": query_id, "v": 3}
    if days:
        params["p"] = min(int(days), 365)
    response = http.get(f"{BASE_URL}/SendRequest", params=params, timeout=30)
    response.raise_for_status()
    root = _xml(response)
    if root.findtext("Status") != "Success":
        raise FlexError(root.findtext("ErrorCode") or "?", root.findtext("ErrorMessage") or "request failed")
    reference = root.findtext("ReferenceCode")

    # 1 request per second, 10 per minute per token: wait between polls.
    for attempt in range(attempts):
        sleep(3 if attempt == 0 else wait)
        response = http.get(f"{BASE_URL}/GetStatement", params={"t": token, "q": reference, "v": 3}, timeout=60)
        response.raise_for_status()
        root = _xml(response)
        if root.tag == "FlexQueryResponse":
            return root
        code = root.findtext("ErrorCode") or "?"
        if code not in RETRY_CODES:
            raise FlexError(code, root.findtext("ErrorMessage") or "statement failed")
    raise FlexError("timeout", "the statement was still being generated; try again later")


# ── parsing ─────────────────────────────────────────────────────────────────

def _money(value):
    try:
        return to_cents(value) if value not in (None, "") else 0
    except (InvalidOperation, ValueError):
        return 0


def _number(value):
    try:
        return float(Decimal(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _date(value):
    """'20261021' or '2026-10-21' (or with ';HHMMSS') -> '2026-10-21'."""
    digits = "".join(ch for ch in (value or "").split(";")[0] if ch.isdigit())
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) == 8 else None


def _summary_rows(parent, tag):
    # Positions can be reported per lot as well; the SUMMARY row (or no level) is the position.
    return [el for el in parent.iter(tag) if el.get("levelOfDetail", "SUMMARY").upper() == "SUMMARY"]


def parse_report(root):
    """One dict per account in the report."""
    accounts = []
    for statement in root.iter("FlexStatement"):
        info = statement.find("AccountInformation")
        base = (info.get("currency") if info is not None else None) or "EUR"
        account = statement.get("accountId") or (info.get("accountId") if info is not None else "") or "IBKR"

        days = []
        for row in statement.iter("EquitySummaryByReportDateInBase"):
            as_of = _date(row.get("reportDate"))
            if not as_of:
                continue
            parts = {key: _money(row.get(key)) for key in ("cash", "stock", "funds", "bonds", "options")}
            total = _money(row.get("total"))
            days.append({"as_of": as_of, **parts, "other": total - sum(parts.values()), "total": total})
        days.sort(key=lambda day: day["as_of"])

        rates = {base: Decimal(1)}
        for rate in statement.iter("ConversionRate"):
            if rate.get("toCurrency") == base and rate.get("fromCurrency"):
                try:
                    rates[rate.get("fromCurrency")] = Decimal(rate.get("rate"))
                except (InvalidOperation, TypeError):
                    pass

        positions = []
        for pos in _summary_rows(statement, "OpenPosition"):
            try:
                fx = Decimal(pos.get("fxRateToBase") or 1)
            except InvalidOperation:
                fx = Decimal(1)
            currency = pos.get("currency") or base
            rates.setdefault(currency, fx)

            def in_base(name):
                try:
                    return _money(Decimal(pos.get(name)) * fx)
                except (InvalidOperation, TypeError):
                    return None

            positions.append({
                "asset_class": pos.get("assetCategory") or "",
                "symbol": pos.get("symbol") or pos.get("conid") or "?",
                "description": pos.get("description") or "",
                "isin": pos.get("isin") or None,
                "currency": currency,
                "quantity": _number(pos.get("position")) or 0.0,
                "price": _number(pos.get("markPrice")),
                "value_cents": _money(pos.get("positionValue")),
                "value_base_cents": in_base("positionValue") or 0,
                "cost_base_cents": in_base("costBasisMoney"),
                "unrealized_base_cents": in_base("fifoPnlUnrealized"),
                "percent_of_nav": _number(pos.get("percentOfNAV")),
            })
        positions.sort(key=lambda p: -p["value_base_cents"])

        cash = []
        for row in statement.iter("CashReportCurrency"):
            currency = row.get("currency") or ""
            if currency.upper().startswith("BASE"):
                continue  # BASE_SUMMARY: the total, already in the NAV rows
            amount = _money(row.get("endingCash"))
            rate = rates.get(currency)
            cash.append({
                "currency": currency,
                "cash_cents": amount,
                "settled_cents": _money(row.get("endingSettledCash")) if row.get("endingSettledCash") else None,
                "base_cents": _money(Decimal(amount) / 100 * rate) if rate is not None else None,
            })

        as_of = _date(statement.get("toDate")) or (days[-1]["as_of"] if days else None)
        if days or positions or cash:
            accounts.append({"account": account, "base": base, "as_of": as_of, "days": days,
                             "positions": positions, "cash": cash})
    return accounts


# ── storing ─────────────────────────────────────────────────────────────────

def store(db, accounts, source="ibkr"):
    """Upsert one snapshot per account and day; positions and cash go on the
    report's last day. Returns the number of days stored."""
    stamp = ts(now())
    stored = 0
    for acct in accounts:
        days = acct["days"] or [{"as_of": acct["as_of"], "cash": sum(c["base_cents"] or 0 for c in acct["cash"]),
                                 "stock": 0, "funds": 0, "bonds": 0, "options": 0, "other": 0,
                                 "total": sum(p["value_base_cents"] for p in acct["positions"])
                                 + sum(c["base_cents"] or 0 for c in acct["cash"])}]
        for day in days:
            db.execute(
                """INSERT INTO holding_snapshots (source, account, as_of, base_currency, cash_cents, stock_cents,
                       funds_cents, bonds_cents, options_cents, other_cents, total_cents, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (source, account, as_of) DO UPDATE SET
                       base_currency = excluded.base_currency, cash_cents = excluded.cash_cents,
                       stock_cents = excluded.stock_cents, funds_cents = excluded.funds_cents,
                       bonds_cents = excluded.bonds_cents, options_cents = excluded.options_cents,
                       other_cents = excluded.other_cents, total_cents = excluded.total_cents,
                       fetched_at = excluded.fetched_at""",
                (source, acct["account"], day["as_of"], acct["base"], day["cash"], day["stock"], day["funds"],
                 day["bonds"], day["options"], day["other"], day["total"], stamp),
            )
            stored += 1
        last = acct["as_of"] or days[-1]["as_of"]
        snapshot_id = db.execute(
            "SELECT id FROM holding_snapshots WHERE source = ? AND account = ? AND as_of = ?",
            (source, acct["account"], last),
        ).fetchone()
        if snapshot_id is None:
            continue
        snapshot_id = snapshot_id[0]
        db.execute("DELETE FROM holding_positions WHERE snapshot_id = ?", (snapshot_id,))
        db.execute("DELETE FROM holding_cash WHERE snapshot_id = ?", (snapshot_id,))
        db.executemany(
            """INSERT INTO holding_positions (snapshot_id, asset_class, symbol, description, isin, currency, quantity,
                   price, value_cents, value_base_cents, cost_base_cents, unrealized_base_cents, percent_of_nav)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(snapshot_id, p["asset_class"], p["symbol"], p["description"], p["isin"], p["currency"], p["quantity"],
              p["price"], p["value_cents"], p["value_base_cents"], p["cost_base_cents"],
              p["unrealized_base_cents"], p["percent_of_nav"]) for p in acct["positions"]],
        )
        db.executemany(
            "INSERT INTO holding_cash (snapshot_id, currency, cash_cents, settled_cents, base_cents) VALUES (?, ?, ?, ?, ?)",
            [(snapshot_id, c["currency"], c["cash_cents"], c["settled_cents"], c["base_cents"]) for c in acct["cash"]],
        )
    db.commit()
    return stored


# ── syncing ─────────────────────────────────────────────────────────────────

def configured():
    cfg = current_app.config
    return bool(cfg["IBKR_FLEX_TOKEN"] and cfg["IBKR_FLEX_QUERY_ID"])


def status(db):
    row = db.execute("SELECT * FROM sync_status WHERE name = 'ibkr'").fetchone()
    return dict(row) if row else {"last_attempt": None, "last_success": None, "last_error": None, "alerted_on": None}


def sync(days=None):
    """Fetch, parse and store; records the outcome. Returns the days stored."""
    db = get_db()
    cfg = current_app.config
    moment = ts(now())
    db.execute("INSERT INTO sync_status (name, last_attempt) VALUES ('ibkr', ?) "
               "ON CONFLICT (name) DO UPDATE SET last_attempt = excluded.last_attempt", (moment,))
    db.commit()
    try:
        accounts = parse_report(fetch_report(cfg["IBKR_FLEX_TOKEN"], cfg["IBKR_FLEX_QUERY_ID"], days=days))
        if not accounts:
            raise FlexError("empty", "the report has no NAV, positions or cash; check the Flex Query sections")
        stored = store(db, accounts)
    except (FlexError, requests.RequestException, ET.ParseError) as exc:
        message = str(exc)
        db.execute("UPDATE sync_status SET last_error = ? WHERE name = 'ibkr'", (message,))
        db.commit()
        log.warning("IBKR sync failed: %s", message)
        _alert_once(db, message)
        raise
    db.execute("UPDATE sync_status SET last_success = ?, last_error = NULL WHERE name = 'ibkr'", (moment,))
    db.commit()
    return stored


def _alert_once(db, message):
    """Tell the admin on Telegram, at most once a day; an expired token needs action."""
    today = now().date().isoformat()
    if status(db).get("alerted_on") != today:
        db.execute("UPDATE sync_status SET alerted_on = ? WHERE name = 'ibkr'", (today,))
        db.commit()
        hint = " Make a new Flex token in Client Portal and update IBKR_FLEX_TOKEN." if "1012" in message or "1015" in message else ""
        telegram.notify_admin(f"IBKR holdings sync failed: {message}.{hint}")


def due(db):
    """Once a day after SYNC_AFTER_HOUR, retrying hourly until it works."""
    if not configured():
        return False
    moment = now()
    if moment.hour < SYNC_AFTER_HOUR:
        return False
    state = status(db)
    if state["last_success"] and state["last_success"][:10] == moment.date().isoformat():
        return False
    return not state["last_attempt"] or state["last_attempt"] < ts(moment - RETRY_EVERY)
