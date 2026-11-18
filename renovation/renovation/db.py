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


# schema.sql is always the full current schema; later entries bring existing
# databases up to date. PRAGMA user_version records how far we got.
MIGRATIONS = [
    _schema,
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
