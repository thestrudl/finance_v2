import xml.etree.ElementTree as ET
from datetime import datetime

import pytest
from conftest import ADMIN, csrf_from, login, texts

from finance import ibkr
from finance.db import get_db

REPORT = b"""<FlexQueryResponse queryName="finance-holdings" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20261020" toDate="20261021" period="Last2BusinessDays" whenGenerated="20261022;083012">
<AccountInformation accountId="U1234567" currency="EUR" name="Ben Novak" accountType="Individual" />
<EquitySummaryInBase>
<EquitySummaryByReportDateInBase accountId="U1234567" currency="EUR" reportDate="20261020" cash="1200.50" stock="30500.00" funds="0" bonds="0" options="0" total="31704.00" />
<EquitySummaryByReportDateInBase accountId="U1234567" currency="EUR" reportDate="20261021" cash="1200.50" stock="30899.50" funds="0" bonds="0" options="0" dividendAccruals="3.10" total="32103.50" />
</EquitySummaryInBase>
<OpenPositions>
<OpenPosition accountId="U1234567" currency="EUR" fxRateToBase="1" assetCategory="STK" symbol="VWCE" description="VANG FTSE AW USDA" isin="IE00BK5BQT80" reportDate="20261021" position="150" markPrice="140.10" positionValue="21015" costBasisMoney="16500" percentOfNAV="65.46" fifoPnlUnrealized="4515" levelOfDetail="SUMMARY" />
<OpenPosition accountId="U1234567" currency="USD" fxRateToBase="0.92" assetCategory="STK" symbol="QQQ" description="INVESCO QQQ TRUST SERIES 1" isin="US46090E1038" reportDate="20261021" position="20" markPrice="537.25" positionValue="10745" costBasisMoney="8000" percentOfNAV="30.79" fifoPnlUnrealized="2745" levelOfDetail="SUMMARY" />
<OpenPosition accountId="U1234567" currency="USD" fxRateToBase="0.92" assetCategory="STK" symbol="QQQ" position="10" positionValue="5372.5" levelOfDetail="LOT" />
</OpenPositions>
<CashReport>
<CashReportCurrency accountId="U1234567" currency="BASE_SUMMARY" endingCash="1200.50" endingSettledCash="1200.50" />
<CashReportCurrency accountId="U1234567" currency="EUR" endingCash="1016.50" endingSettledCash="1016.50" />
<CashReportCurrency accountId="U1234567" currency="USD" endingCash="200" endingSettledCash="150" />
</CashReport>
<ConversionRates>
<ConversionRate reportDate="20261021" fromCurrency="USD" toCurrency="EUR" rate="0.92" />
</ConversionRates>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""

SENT = b"<FlexStatementResponse timestamp='x'><Status>Success</Status><ReferenceCode>1234567890</ReferenceCode><Url>ignored</Url></FlexStatementResponse>"
WAIT = b"<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode><ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage></FlexStatementResponse>"
EXPIRED = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"


class FakeHttp:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[1], params))
        body = self.bodies.pop(0)

        class Response:
            content = body

            def raise_for_status(self):
                pass

        return Response()


def no_sleep(_seconds):
    pass


def test_parse_report():
    [acct] = ibkr.parse_report(ET.fromstring(REPORT))
    assert (acct["account"], acct["base"], acct["as_of"]) == ("U1234567", "EUR", "2026-10-21")
    assert [d["as_of"] for d in acct["days"]] == ["2026-10-20", "2026-10-21"]
    last = acct["days"][-1]
    assert (last["cash"], last["stock"], last["total"], last["other"]) == (120050, 3089950, 3210350, 350)

    vwce, qqq = acct["positions"]  # the LOT row is left out
    assert (vwce["symbol"], vwce["value_base_cents"], vwce["cost_base_cents"], vwce["unrealized_base_cents"]) == \
        ("VWCE", 2101500, 1650000, 451500)
    assert (qqq["currency"], qqq["value_cents"], qqq["value_base_cents"], qqq["unrealized_base_cents"]) == \
        ("USD", 1074500, 988540, 252540)
    assert qqq["quantity"] == 20 and qqq["price"] == 537.25

    cash = {c["currency"]: c for c in acct["cash"]}
    assert set(cash) == {"EUR", "USD"}  # BASE_SUMMARY is the total, not a currency
    assert (cash["USD"]["cash_cents"], cash["USD"]["settled_cents"], cash["USD"]["base_cents"]) == (20000, 15000, 18400)


def test_fetch_waits_for_the_statement():
    http = FakeHttp(SENT, WAIT, REPORT)
    root = ibkr.fetch_report("tok", "999", days=400, http=http, sleep=no_sleep)
    assert root.tag == "FlexQueryResponse"
    assert http.calls[0] == ("SendRequest", {"t": "tok", "q": "999", "v": 3, "p": 365})
    assert http.calls[1] == ("GetStatement", {"t": "tok", "q": "1234567890", "v": 3})
    assert len(http.calls) == 3


def test_fetch_reports_ibkr_errors():
    with pytest.raises(ibkr.FlexError) as err:
        ibkr.fetch_report("tok", "999", http=FakeHttp(EXPIRED), sleep=no_sleep)
    assert err.value.code == "1012"


def test_fetch_needs_xml():
    with pytest.raises(ibkr.FlexError, match="XML"):
        ibkr.fetch_report("tok", "999", http=FakeHttp(SENT, b'"ClientAccountID","Symbol"'), sleep=no_sleep)


def test_fetch_gives_up_politely():
    with pytest.raises(ibkr.FlexError, match="still being generated"):
        ibkr.fetch_report("tok", "999", attempts=2, http=FakeHttp(SENT, WAIT, WAIT), sleep=no_sleep)


def test_store_is_repeatable(app):
    accounts = ibkr.parse_report(ET.fromstring(REPORT))
    with app.app_context():
        db = get_db()
        assert ibkr.store(db, accounts) == 2
        ibkr.store(db, accounts)
        assert db.execute("SELECT COUNT(*) FROM holding_snapshots").fetchone()[0] == 2
        latest = db.execute("SELECT id FROM holding_snapshots WHERE as_of = '2026-10-21'").fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM holding_positions WHERE snapshot_id = ?", (latest,)).fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM holding_cash").fetchone()[0] == 2


@pytest.fixture
def configured(app, monkeypatch):
    app.config.update(IBKR_FLEX_TOKEN="tok", IBKR_FLEX_QUERY_ID="999")
    monkeypatch.setattr(ibkr, "fetch_report", lambda token, query, days=None: ET.fromstring(REPORT))
    return app


def test_sync_records_success(configured):
    with configured.app_context():
        assert ibkr.sync() == 2
        state = ibkr.status(get_db())
        assert state["last_success"] and state["last_error"] is None


def test_failed_sync_alerts_admin_once_a_day(app, sent, monkeypatch):
    app.config.update(IBKR_FLEX_TOKEN="tok", IBKR_FLEX_QUERY_ID="999")

    def expired(*_args, **_kwargs):
        raise ibkr.FlexError("1012", "Token has expired.")

    monkeypatch.setattr(ibkr, "fetch_report", expired)
    with app.app_context():
        for _ in range(2):
            with pytest.raises(ibkr.FlexError):
                ibkr.sync()
        assert "1012" in ibkr.status(get_db())["last_error"]
    alerts = texts(sent, str(ADMIN))
    assert len(alerts) == 1 and "new Flex token" in alerts[0]


def test_due(app, monkeypatch):
    with app.app_context():
        db = get_db()
        assert not ibkr.due(db)  # not configured
        app.config.update(IBKR_FLEX_TOKEN="tok", IBKR_FLEX_QUERY_ID="999")
        monkeypatch.setattr(ibkr, "now", lambda: datetime(2026, 10, 22, 6, 30))
        assert not ibkr.due(db)  # too early
        monkeypatch.setattr(ibkr, "now", lambda: datetime(2026, 10, 22, 8, 0))
        assert ibkr.due(db)
        db.execute("INSERT INTO sync_status (name, last_attempt) VALUES ('ibkr', '2026-10-22 07:30:00')")
        db.commit()
        assert not ibkr.due(db)  # failed half an hour ago: wait for the hourly retry
        monkeypatch.setattr(ibkr, "now", lambda: datetime(2026, 10, 22, 8, 31))
        assert ibkr.due(db)
        db.execute("UPDATE sync_status SET last_success = '2026-10-22 08:31:00'")
        db.commit()
        assert not ibkr.due(db)  # done for today


def test_holdings_page_before_setup(client, outbox):
    login(client, outbox)
    html = client.get("/holdings").get_data(as_text=True)
    assert "Connect Interactive Brokers" in html and "IBKR_FLEX_TOKEN" in html


def test_holdings_page_and_sync_now(configured, client, outbox):
    login(client, outbox)
    page = client.get("/holdings")
    assert "No report stored yet" in page.get_data(as_text=True)
    client.post("/holdings/sync", data={"csrf": csrf_from(page), "history": "1"})
    html = client.get("/holdings").get_data(as_text=True)
    assert "€32,103.50" in html          # account value
    assert "€1,200.50" in html           # cash
    assert "VWCE" in html and "QQQ" in html
    assert "+€7,040.40" in html          # unrealized: 4,515 + 2,745 × 0.92
    assert "€184.00" in html             # USD cash in EUR
    page = client.get("/holdings")
    client.post("/holdings/sync", data={"csrf": csrf_from(page)})
    assert "less than a minute ago" in client.get("/holdings").get_data(as_text=True)


def test_cli_reads_a_saved_report(app, tmp_path):
    path = tmp_path / "flex.xml"
    path.write_bytes(REPORT)
    result = app.test_cli_runner().invoke(args=["ibkr-sync", "--file", str(path)], catch_exceptions=False)
    assert "U1234567 on 2026-10-21: value 32,103.50 EUR, cash 1,200.50, 2 positions" in result.output
