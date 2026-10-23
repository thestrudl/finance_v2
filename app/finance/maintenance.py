import sqlite3
from pathlib import Path

from flask import current_app

from .db import connect


def backup(today, force=False):
    """Daily copy of the database into BACKUP_DIR, keeping the newest BACKUP_KEEP.
    Returns the new file's path, or None if today's copy already exists."""
    cfg = current_app.config
    folder = Path(cfg["BACKUP_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"finance-{today.isoformat()}.db"
    if target.exists() and not force:
        return None
    partial = target.with_suffix(".partial")
    source = connect(cfg["DATABASE_PATH"])
    copy = sqlite3.connect(partial)
    try:
        source.backup(copy)  # consistent snapshot, safe while the app writes
    finally:
        copy.close()
        source.close()
    partial.replace(target)
    for old in sorted(folder.glob("finance-*.db"))[: -cfg["BACKUP_KEEP"]]:
        old.unlink()
    return target
