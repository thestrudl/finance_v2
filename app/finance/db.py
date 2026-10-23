"""SQLite storage. One file on the /data volume; WAL lets the web workers and
the scheduler container read and write at the same time."""
import sqlite3
from importlib import resources
from pathlib import Path

from flask import current_app, g


def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_PATH"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# schema.sql is always the full current schema (CREATE ... IF NOT EXISTS), so
# a new database is complete after migration 1; later entries bring existing
# databases up to date. Each runs once, in order; PRAGMA user_version records
# how far we got. Statements must be idempotent: the web app and the scheduler
# can both start on the same database at the same moment.
def _schema(conn, household=None):
    conn.executescript(resources.files(__package__).joinpath("schema.sql").read_text())


def _migration_1(conn, household):
    _schema(conn)
    from .categories import seed

    seed(conn, household)


def _migration_2(conn, household=None):
    # Password logins replaced by emailed codes; no accounts worth keeping.
    conn.executescript("DROP TABLE IF EXISTS login_failures; DROP TABLE IF EXISTS users;")
    _schema(conn)


def _migration_4(conn, household):
    # Household category clean-up, and corrected typos dropped.
    from .categories import cleanup
    from .importer import drop_reversed_pairs

    cleanup(conn, household)
    drop_reversed_pairs(conn)


def _migration_6(conn, household=None):
    # Balance checks, and the "outside our accounts" flag on transactions.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
    if "off_balance" not in columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN off_balance INTEGER NOT NULL DEFAULT 0")
    _schema(conn)


MIGRATIONS = [
    _migration_1,
    _migration_2,
    _schema,  # 3: pending_entries for the "Did you mean ...?" typo check
    _migration_4,
    _schema,  # 5: holdings (IBKR Flex snapshots) and sync_status
    _migration_6,
]


def migrate(conn, household):
    conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, step in enumerate(MIGRATIONS[version:], start=version + 1):
        step(conn, household)
        conn.execute(f"PRAGMA user_version = {number}")
        conn.commit()


def init_app(app):
    path = Path(app.config["DATABASE_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        from .categories import load_household

        migrate(conn, load_household(app.config.get("HOUSEHOLD_FILE")))
    finally:
        conn.close()
    app.teardown_appcontext(close_db)
