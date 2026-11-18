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


# schema.sql is always the full current schema; later entries bring existing
# databases up to date. PRAGMA user_version records how far we got.
MIGRATIONS = [
    _schema,
    _migration_2,
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
