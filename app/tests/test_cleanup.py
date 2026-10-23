from conftest import add_tx, csrf_from, login, message_update, post_update

from finance import categories, importer
from finance.db import get_db

CSV = (
    "date time,User,Price,Category,Comment,Income true false\r\n"
    "1/20/2025 10:00:00,Ben N,2500,salary,placa,TRUE\r\n"
    "2/20/2025 10:00:00,,2510,placa,placa,TRUE\r\n"
    "3/1/2025 10:00:00,,428,tax,tax return,TRUE\r\n"
    "3/2/2025 10:00:00,,15,Lekarna,probiotiki,FALSE\r\n"
    "3/3/2025 10:00:00,,10,farmacija,,FALSE\r\n"
    "3/4/2025 10:00:00,,1500,firma,office costs,FALSE\r\n"
    "4/7/2025 20:17:29,Ana Novak,30,In,birthday,FALSE\r\n"
    "4/7/2025 20:17:44,Ana Novak,-30,In,birthday,FALSE\r\n"
    "6/19/2025 12:47:20,,5,Food,kruh,FALSE\r\n"
    "6/19/2025 12:47:27,,-5,Food,kruh,FALSE\r\n"
    "6/20/2025 09:00:00,,-7.86,Bike,refund,FALSE\r\n"
)


def categories_of(app):
    with app.app_context():
        return {r["category_raw"]: r["category"] for r in get_db().execute("SELECT * FROM transactions")}


def group(app, key):
    with app.app_context():
        return get_db().execute("SELECT grp FROM categories WHERE key = ?", (key,)).fetchone()[0]


def test_import_lands_in_the_cleaned_up_categories(app):
    result = app.test_cli_runner().invoke(args=["import-csv", "-"], input=CSV, catch_exceptions=False)
    assert "transactions: 7" in result.output and "reversed pairs dropped: 2" in result.output
    assert categories_of(app) == {
        "salary": "placa", "placa": "placa", "tax": "dohodnina",
        "Lekarna": "zdravje", "farmacija": "zdravje", "firma": "firma", "Bike": "bike",
    }
    assert group(app, "firma") == "business" and group(app, "app") == "business"
    with app.app_context():
        income = get_db().execute("SELECT SUM(amount_cents) FROM transactions WHERE is_income").fetchone()[0]
    assert income == 543800  # dropping the reversed pairs changes no total


def test_cleanup_also_fixes_data_already_in_the_database(app):
    add_tx(app, "2025-01-20 10:00:00", 250000, "salary", is_income=True, source="import")
    add_tx(app, "2025-03-02 10:00:00", 1500, "zobar", source="import")
    with app.app_context():
        db = get_db()
        # As if imported before the clean-up existed: put them back under their own spelling.
        db.execute("DELETE FROM category_aliases WHERE alias IN ('salary', 'zobar')")
        db.execute("INSERT OR IGNORE INTO categories (key, label) VALUES ('salary', 'Salary'), ('zobar', 'Zobar')")
        db.execute("UPDATE transactions SET category = lower(category_raw)")
        db.commit()
        categories.cleanup(db, categories.load_household(app.config["HOUSEHOLD_FILE"]))
        db.commit()
        assert {r[0] for r in db.execute("SELECT category FROM transactions")} == {"placa", "zdravje"}
        assert db.execute("SELECT COUNT(*) FROM categories WHERE key IN ('salary', 'zobar')").fetchone()[0] == 0


def test_telegram_messages_follow_the_cleanup(app, client, sent):
    post_update(client, message_update("in salary 2600 oktober"))
    with app.app_context():
        assert get_db().execute("SELECT category FROM transactions").fetchone()[0] == "placa"


def test_only_imported_pairs_are_dropped(app):
    add_tx(app, "2026-10-01 10:00:00", 500, "food", source="telegram")
    add_tx(app, "2026-10-01 10:01:00", -500, "food", source="telegram")
    with app.app_context():
        assert importer.drop_reversed_pairs(get_db()) == 0


def test_split_off_undoes_a_merge(app, client, outbox, sent):
    app.test_cli_runner().invoke(args=["import-csv", "-"], input=CSV, catch_exceptions=False)
    login(client, outbox)
    page = client.get("/category/placa")
    assert "Split off" in page.get_data(as_text=True)
    client.post("/categories/placa/split", data={"csrf": csrf_from(page), "alias": "salary"})
    assert categories_of(app)["salary"] == "salary" and categories_of(app)["placa"] == "placa"
    post_update(client, message_update("in salary 100"))
    with app.app_context():
        assert get_db().execute("SELECT category FROM transactions ORDER BY id DESC").fetchone()[0] == "salary"


def test_split_off_ignores_a_spelling_of_another_category(app, client, outbox):
    login(client, outbox)
    page = client.get("/category/placa")
    client.post("/categories/placa/split", data={"csrf": csrf_from(page), "alias": "zobar"})
    with app.app_context():
        assert get_db().execute("SELECT category_key FROM category_aliases WHERE alias = 'zobar'").fetchone()[0] == "zdravje"


def test_a_fresh_install_uses_the_public_example(tmp_path):
    from pathlib import Path

    from conftest import make_app

    example = Path(__file__).resolve().parent.parent / "household.example.json"
    app = make_app(tmp_path, HOUSEHOLD_FILE=str(example))
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT grp FROM categories WHERE key = 'rental'").fetchone()[0] == "business"
        assert db.execute("SELECT category_key FROM category_aliases WHERE alias = 'pharmacy'").fetchone()[0] == "health"
        assert db.execute("SELECT COUNT(*) FROM categories WHERE key = 'food'").fetchone()[0] == 1  # built-in default
