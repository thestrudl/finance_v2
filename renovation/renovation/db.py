"""SQLite storage: one file on the renovation-data volume, separate from finance."""
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


def _schema(conn):
    conn.executescript(resources.files(__package__).joinpath("schema.sql").read_text())


def _migration_2(conn):
    # Payments and commitments; a task's "paid so far" becomes a payment that
    # was already in finance.
    _schema(conn)
    if "paid_cents" in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}:
        conn.execute(
            """INSERT INTO payments (goal_id, task_id, paid_on, amount_cents, what, finance, created_at)
               SELECT goal_id, id, COALESCE(done_on, started_on, date('now')), paid_cents,
                      'Paid before payments were listed', 'skip', updated_at
               FROM tasks WHERE paid_cents <> 0"""
        )
        conn.execute("ALTER TABLE tasks DROP COLUMN paid_cents")


PAYMENTS_COLS = """    goal_id       INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    commitment_id INTEGER REFERENCES commitments(id) ON DELETE SET NULL,
    paid_on       TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents <> 0),  -- negative: a refund
    what          TEXT NOT NULL DEFAULT '',
    off_balance   INTEGER NOT NULL DEFAULT 0,      -- paid outside our accounts (a loan paid to the seller)
    finance       TEXT NOT NULL DEFAULT 'skip' CHECK (finance IN ('logged', 'failed', 'skip', 'linked')),
    finance_key   TEXT UNIQUE,
    finance_tx_id INTEGER UNIQUE,                  -- 'linked': the finance transaction it is
    finance_error TEXT,
    user_name     TEXT,
    created_at    TEXT NOT NULL"""


def _migration_3(conn):
    # Payments linked from finance (a new 'linked' state: SQLite can't change
    # a CHECK, so the table is rebuilt), and dependencies on whole big items.
    if "finance_tx_id" not in {row[1] for row in conn.execute("PRAGMA table_info(payments)")}:
        conn.commit()
        conn.executescript(f"""
            BEGIN;
            CREATE TABLE payments_new (
                id            INTEGER PRIMARY KEY,
{PAYMENTS_COLS}
            );
            INSERT INTO payments_new (id, goal_id, task_id, commitment_id, paid_on, amount_cents, what, off_balance,
                                      finance, finance_key, finance_error, user_name, created_at)
                SELECT id, goal_id, task_id, commitment_id, paid_on, amount_cents, what, off_balance,
                       finance, finance_key, finance_error, user_name, created_at FROM payments;
            DROP TABLE payments;
            ALTER TABLE payments_new RENAME TO payments;
            COMMIT;
        """)
    _schema(conn)


# schema.sql is always the full current schema; later entries bring existing
# databases up to date. PRAGMA user_version records how far we got.
MIGRATIONS = [
    _schema,
    _migration_2,
    _migration_3,
    _schema,  # 4: digest_runs (the twice-weekly email)
]


def migrate(conn):
    conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, step in enumerate(MIGRATIONS[version:], start=version + 1):
        step(conn)
        conn.execute(f"PRAGMA user_version = {number}")
        conn.commit()


def init_app(app):
    path = Path(app.config["DATABASE_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        migrate(conn)
    finally:
        conn.close()
    app.teardown_appcontext(close_db)
