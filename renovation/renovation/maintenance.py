"""Background jobs, run as their own small container (python -m
renovation.maintenance) so the web workers never do them twice: the
"ready to start" email on its schedule, the daily backup, pruning."""
import logging
import signal
import sqlite3
import sys
import time
from datetime import timedelta
from pathlib import Path

from flask import current_app

from . import digest
from .db import connect, get_db
from .util import now, ts

log = logging.getLogger("renovation.maintenance")

INTERVAL_SECONDS = 300  # the email goes out within 5 minutes of its time


# RETIRE after central backup runner has 7 successful nights (backup contract)
def backup(day, force=False):
    """Copy of the database into BACKUP_DIR, keeping the newest BACKUP_KEEP.
    Returns the new file's path, or None if that day's copy already exists."""
    cfg = current_app.config
    folder = Path(cfg["BACKUP_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"renovation-{day.isoformat()}.db"
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
    for old in sorted(folder.glob("renovation-*.db"))[: -cfg["BACKUP_KEEP"]]:
        old.unlink()
    return target


def tick(app):
    with app.app_context():
        try:
            if slot := digest.run_due(now()):
                log.info("Sent the digest for %s", slot)
        except Exception:
            log.exception("The digest email failed; trying again at the next check")
        if path := backup(now().date()):
            log.info("Backup written to %s", path)
        db = get_db()
        cutoff = ts(now() - timedelta(days=7))
        db.execute("DELETE FROM login_events WHERE at < ?", (cutoff,))
        db.execute("DELETE FROM login_tokens WHERE expires_at < ?", (cutoff,))
        db.commit()


def main():
    from . import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    app = create_app()
    log.info("Maintenance started")
    while True:
        try:
            tick(app)
        except Exception:
            log.exception("Maintenance tick failed")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
