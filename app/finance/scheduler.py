"""Background jobs, run as their own container (python -m finance.scheduler)
so the web workers never run them twice: repeat entries, the daily backup,
pruning old bookkeeping rows (update ids, login events and tokens), and the
morning IBKR holdings sync."""
import logging
import signal
import sys
import time
import traceback
from datetime import timedelta

from . import create_app, ibkr, maintenance, repeats, telegram
from .db import get_db
from .util import now, ts

log = logging.getLogger("finance.scheduler")

INTERVAL_SECONDS = 600
ALERT_EVERY = timedelta(hours=1)


def tick(app):
    with app.app_context():
        today = now().date()
        db = get_db()
        created = repeats.run_due(db, today)
        if created:
            log.info("Logged %d repeat entries", len(created))
        if path := maintenance.backup(today):
            log.info("Backup written to %s", path)
        if ibkr.due(db):
            first = db.execute("SELECT 1 FROM holding_snapshots WHERE source = 'ibkr' LIMIT 1").fetchone() is None
            try:
                stored = ibkr.sync(days=365 if first else None)  # the first run loads a year of history
                log.info("IBKR holdings synced (%d days)", stored)
            except Exception:
                pass  # recorded in sync_status, admin told once a day
        cutoff = ts(now() - timedelta(days=7))
        db.execute("DELETE FROM telegram_updates WHERE received_at < ?", (cutoff,))
        db.execute("DELETE FROM login_events WHERE at < ?", (cutoff,))
        db.execute("DELETE FROM login_tokens WHERE expires_at < ?", (cutoff,))
        db.execute("DELETE FROM pending_entries WHERE created_at < ?", (cutoff,))
        db.commit()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # PID 1 in a container ignores SIGTERM unless a handler is installed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    app = create_app()
    last_alert = None
    log.info("Scheduler started")
    while True:
        try:
            tick(app)
        except Exception:
            log.exception("Scheduler tick failed")
            with app.app_context():
                if last_alert is None or now() - last_alert > ALERT_EVERY:
                    last_alert = now()
                    telegram.notify_admin("Scheduler error:\n" + traceback.format_exc()[-3000:])
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
