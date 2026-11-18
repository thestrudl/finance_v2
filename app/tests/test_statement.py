from datetime import date

from conftest import add_tx

from finance import statement
from finance.db import get_db

HEADER = ("ŠT. IZPISKA;POGODBA;RAČUN;DATUM KNJIŽENJA;DATUM VALUTE;DOBRO;BREME;VALUTA;NAMEN;SKLIC V DOBRO;"
          "SKLIC V BREME;UDELEŽENEC - RAČUN;UDELEŽENEC - NAZIV;UDELEŽENEC - BIC;KODA NAMENA;"
          "PRILIV V IZVORNI VALUTI;ODLIV V IZVORNI VALUTI;IZVORNA VALUTA")


def nlb(*rows):
    lines = [HEADER]
    for posted, value, credit, debit, purpose, payee in rows:
        lines.append(f"0;SI56;SI56;{posted};{value};{credit};{debit};EUR;{purpose};;;SI56;{payee};;;;;")
    return "\r\n".join(lines).encode("cp1250")


STATEMENT = nlb(
    ("19.10.2026", "19.10.2026", "", "87,89", "PLAČILO RAČUNA ŠT. 123456789", "CITY POWER D.D."),
    ("18.10.2026", "18.10.2026", "", "1.000,00", "Trajnik za plačilo obveznosti", "BANKA d.d."),
    ("19.10.2026", "19.10.2026", "", "2.500,00", "PLAČILO", "ANA NOVAK"),
    ("17.11.2026", "15.11.2026", "", "174,51", "MOTO SHOP S.R.O.", "MOTO SHOP S.R.O."),
    ("21.10.2026", "21.10.2026", "", "249,83", "PLAČILO TUJINA", "INSURER D.D."),
    ("22.10.2026", "22.10.2026", "650,00", "", "NAKAZILO", "STRANKA"),
)
RULES = [
    {"match": "ana novak", "skip": "own transfer"},
    {"match": "CITY POWER", "category": "elektrika", "comment": "power bill"},
]


def test_reads_the_nlb_export():
    lines = statement.read_nlb(STATEMENT)
    assert [(line.day, line.cents) for line in lines] == [
        (date(2026, 10, 18), 1000_00), (date(2026, 10, 19), 87_89), (date(2026, 10, 19), 2500_00),
        (date(2026, 10, 21), 249_83), (date(2026, 10, 22), -650_00),
        (date(2026, 11, 15), 174_51),  # the value date: when the card was used
    ]
    assert lines[-1].text == "MOTO SHOP S.R.O."
    assert lines[1].text == "CITY POWER D.D. · PLAČILO RAČUNA ŠT. 123456789"
    assert lines[3].text == "INSURER D.D. · PLAČILO TUJINA"
    assert lines[4].is_income


def test_rejects_other_files():
    try:
        statement.read_nlb(b"Date,Amount\n1.1.2026,5\n")
    except ValueError as exc:
        assert "Not an NLB statement" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def compare(app, data=STATEMENT, rules=RULES):
    with app.app_context():
        lines = statement.apply_rules(statement.read_nlb(data), rules)
        return {r.line.payee: r for r in statement.compare(get_db(), lines)}


def test_compare_finds_what_was_not_logged(app):
    add_tx(app, "2026-10-18 03:00:00", 1000_00, "hisa", comment="kredit")
    add_tx(app, "2026-10-21 12:44:00", 249_00, "insurance", comment="tujina")    # rounded: close
    add_tx(app, "2026-11-14 18:00:00", 174_51, "moto")
    add_tx(app, "2026-10-22 09:00:00", 650_00, "app", is_income=True)
    add_tx(app, "2026-09-01 09:00:00", 87_89, "food")                          # too long before
    add_tx(app, "2026-10-19 18:00:00", 87_00, "gas", comment="Petrol")          # fuel, not the electricity bill
    results = compare(app)
    assert {payee: r.status for payee, r in results.items()} == {
        "BANKA d.d.": "logged",
        "CITY POWER D.D.": "missing",
        "ANA NOVAK": "skipped",
        "MOTO SHOP S.R.O.": "logged",
        "INSURER D.D.": "close",
        "STRANKA": "logged",
    }
    assert results["INSURER D.D."].entry["amount_cents"] == 249_00
    assert results["CITY POWER D.D."].line.rule["category"] == "elektrika"


def test_each_entry_matches_one_line(app):
    data = nlb(("30.08.2026", "28.08.2026", "", "2,10", "Petrol GO", "Petrol GO"),
               ("30.08.2026", "28.08.2026", "", "2,10", "Petrol GO", "Petrol GO"))
    add_tx(app, "2026-08-28 10:00:00", 2_10, "drinks")
    with app.app_context():
        results = statement.compare(get_db(), statement.read_nlb(data))
    assert [r.status for r in results] == ["logged", "missing"]


def test_one_entry_paid_in_parts(app):
    data = nlb(("27.10.2026", "27.10.2026", "", "500,00", "PLACILO POGODBE", "KITCHEN STUDIO D.O.O."),
               ("28.10.2026", "28.10.2026", "", "500,00", "PLACILO POGODBE", "KITCHEN STUDIO D.O.O."),
               ("28.10.2026", "28.10.2026", "", "27,28", "PLAČILO STORITEV", "WATERWORKS D.O.O."))
    kitchen = add_tx(app, "2026-10-27 20:45:55", 1000_00, "hisa", comment="predplacilo kuhinja")
    with app.app_context():
        results = statement.compare(get_db(), statement.read_nlb(data))
    assert [(r.status, r.entry and r.entry["id"]) for r in results] == [
        ("split", kitchen), ("split", kitchen), ("missing", None)]


def test_one_entry_paid_at_two_tills_and_rounded(app):
    data = nlb(("06.11.2026", "04.11.2026", "", "6,59", "BAUHAUS BLAGAJNA 10", "BAUHAUS BLAGAJNA 10"),
               ("06.11.2026", "04.11.2026", "", "148,69", "BAUHAUS d.o.o., k.d. 9", "BAUHAUS d.o.o., k.d. 9"))
    add_tx(app, "2026-11-02 08:11:06", 7_00, "hisa", comment="nastavki")  # close to 6.59, but not it
    whole = add_tx(app, "2026-11-04 17:52:26", 155_00, "hisa", comment="bauhaus elektrika")
    with app.app_context():
        results = statement.compare(get_db(), statement.read_nlb(data))
    assert [(r.status, r.entry["id"]) for r in results] == [("split", whole), ("split", whole)]


def test_one_payment_for_several_entries(app):
    data = nlb(("16.09.2026", "14.09.2026", "", "776,48", "AMAZON* NM2MW8OC4", "AMAZON* NM2MW8OC4"),
               ("20.09.2026", "20.09.2026", "", "50,00", "PLAČILO", "NEKDO"))
    parts = [add_tx(app, "2026-09-13 10:00:00", 500_00, "hisa", comment="stedilnik"),
             add_tx(app, "2026-09-14 10:00:00", 276_48, "hisa", comment="napa")]
    add_tx(app, "2026-09-14 11:00:00", 30_00, "food")
    add_tx(app, "2026-09-10 11:00:00", 20_00, "food")   # with the 30 it makes 50, but too long before
    add_tx(app, "2026-09-20 11:00:00", 20_00, "drinks")  # with the 30 it makes 50, but another category
    add_tx(app, "2026-09-13 11:00:00", 49_50, "food")   # 49.50 + 0.50 is rounded, but not logged together
    add_tx(app, "2026-09-14 18:00:00", 50, "food")
    add_tx(app, "2026-09-17 11:00:00", 25_00, "food")   # 25 + 25 = 50, but logged days apart
    add_tx(app, "2026-09-19 11:00:00", 25_00, "food")
    with app.app_context():
        results = statement.compare(get_db(), statement.read_nlb(data))
    assert results[0].status == "combined"
    assert sorted(e["id"] for e in results[0].entries) == sorted(parts)
    assert results[1].status == "missing"


def test_entry_logged_with_the_wrong_sign(app):
    data = nlb(("14.08.2026", "14.08.2026", "", "70,00", "DIREKTNA BREMENITEV (SDD)", "STRIPE"))
    refund = add_tx(app, "2026-08-13 12:43:11", -70_00, "sunny", comment="stripe reverz")
    with app.app_context():
        [result] = statement.compare(get_db(), statement.read_nlb(data))
    assert (result.status, result.entry["id"]) == ("reversed", refund)


def test_one_order_logged_in_two_categories(app):
    data = nlb(("16.11.2026", "16.11.2026", "", "217,40", "AMAZON", "POS terminal"),
               ("20.11.2026", "20.11.2026", "", "97,00", "AMAZON", "POS terminal"))
    order = [add_tx(app, "2026-11-16 13:31:31", 130_00, "hisa", comment="elektronika"),
             add_tx(app, "2026-11-16 13:31:36", 87_00, "app", comment="likovno ustvarjanje")]  # 217.00, rounded
    add_tx(app, "2026-11-19 09:00:00", 50_00, "food")   # 50 + 47 = 97, but different categories hours apart
    add_tx(app, "2026-11-19 18:00:00", 47_00, "gas")
    with app.app_context():
        results = statement.compare(get_db(), statement.read_nlb(data))
    # 217.00 logged for 217.40: shown for review, not taken as certain.
    assert results[0].status == "rounded_sum" and sorted(e["id"] for e in results[0].entries) == sorted(order)
    assert results[1].status == "missing"


def test_missing_lines_go_back_in_with_import_csv_append(app):
    add_tx(app, "2026-10-01 10:00:00", 5_00, "food", source="import")
    results = compare(app).values()
    csv_text = statement.missing_csv(results)
    rows = csv_text.strip().split("\r\n")
    assert rows[0] == "Date time,User,Price,Category,Comment,Income"
    assert "2026-10-19 12:00:00,,87.89,elektrika,power bill,FALSE" in rows
    filled = "\r\n".join([rows[0]] + [row for row in rows if ",elektrika," in row])
    runner = app.test_cli_runner()
    result = runner.invoke(args=["import-csv", "--append", "-"], input=filled.encode())
    assert result.exit_code == 0, result.output
    with app.app_context():
        added = get_db().execute("SELECT * FROM transactions WHERE category = 'elektrika'").fetchone()
        assert (added["amount_cents"], added["source"], added["occurred_at"]) == (87_89, "web", "2026-10-19 12:00:00")
        # The earlier import is untouched.
        assert get_db().execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 1
    # The other lines have no category yet, so they can't go in.
    unfilled = runner.invoke(args=["import-csv", "--append", "-"], input=csv_text.encode())
    assert unfilled.exit_code != 0 and "needs a category" in unfilled.output


def test_check_statement_command(app, tmp_path):
    path = tmp_path / "promet.csv"
    path.write_bytes(STATEMENT)
    add_tx(app, "2026-10-18 03:00:00", 1000_00, "hisa", comment="kredit")
    result = app.test_cli_runner().invoke(args=["check-statement", str(path)])
    assert result.exit_code == 0, result.output
    assert "MISS 19.10.      87.89  CITY POWER D.D. · PLAČILO RAČUNA ŠT. 123456789" in result.output
    assert "BANKA d.d." not in result.output   # logged: only shown with --all
    # The test household has no statement rules, so the own transfer isn't skipped.
    assert "5 missing, 0 with the wrong sign, 0 with a different amount, 0 adding up to other entries, 1 logged, 0 skipped." in result.output
    csv_only = app.test_cli_runner().invoke(args=["check-statement", "--csv", "-", str(path)])
    assert csv_only.output.startswith("Date time,User,Price,Category,Comment,Income")
