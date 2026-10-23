"""Server commands, run inside the container:

    docker compose exec finance flask send-test-email you@example.com
    docker compose exec -T finance flask import-csv - < webHookData.csv
    docker compose exec -T finance flask import-csv --repeats - < repeatData.csv
    docker compose exec finance flask set-webhook
"""
import io
import sys
import xml.etree.ElementTree as ET

import click

from . import holdings, ibkr, importer, mail, maintenance, repeats, telegram
from .db import get_db
from .util import now


def register(app):
    @app.cli.command("list-users")
    def list_users():
        """Who can log in (set PORTAL_USERS in .env to change it)."""
        for user in get_db().execute("SELECT * FROM users ORDER BY id"):
            click.echo(f"{user['name']} <{user['email']}>  last login: {user['last_login_at'] or 'never'}")

    @app.cli.command("send-test-email")
    @click.argument("address")
    def send_test_email(address):
        """Check the SMTP settings by sending a short email."""
        cfg = app.config
        missing = [name for name in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_FROM") if not cfg[name]]
        if missing:
            raise click.ClickException("Not set: " + ", ".join(missing))
        mail.deliver(mail.build(address, "Finance test email",
                                f"SMTP works: sent through {cfg['SMTP_HOST']}:{cfg['SMTP_PORT']} as {cfg['SMTP_USERNAME']}."))
        click.echo(f"Sent to {address}.")

    @app.cli.command("import-ods")
    @click.argument("path")
    @click.option("--replace", is_flag=True, help="Replace transactions from an earlier import.")
    def import_ods(path, replace):
        """Import the Google Sheet export (.ods). PATH '-' reads standard input."""
        source = io.BytesIO(sys.stdin.buffer.read()) if path == "-" else path
        sheets = importer.read_ods(source)
        db = get_db()
        try:
            summary = importer.import_workbook(db, sheets, now().date(), replace)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        for key, count in summary.items():
            click.echo(f"{key}: {count}")
        checked, mismatches = importer.compare_with_year_sheets(db, sheets)
        click.echo(f"Checked {checked} months against the yearly sheets: {len(mismatches)} differ.")
        for ym, sheet_in, db_in, sheet_out, db_out in mismatches:
            click.echo(f"  {ym}: income sheet {sheet_in / 100:.2f} db {db_in / 100:.2f}, "
                       f"expenses sheet {sheet_out / 100:.2f} db {db_out / 100:.2f}")

    @app.cli.command("import-csv")
    @click.argument("path")
    @click.option("--repeats", is_flag=True, help="The file is the repeatData tab, not webHookData.")
    @click.option("--replace", is_flag=True, help="Replace transactions from an earlier import.")
    @click.option("--date-order", type=click.Choice(["mdy", "dmy"]),
                  help="Only needed if the dates can't tell (5/4/2024 could be either).")
    def import_csv(path, repeats, replace, date_order):
        """Import one tab of the Google Sheet saved as .csv. PATH '-' reads standard input."""
        data = sys.stdin.buffer.read() if path == "-" else open(path, "rb").read()
        rows = importer.read_csv(data)
        db = get_db()
        try:
            if repeats:
                summary = importer.import_repeats(db, importer.csv_repeats(rows), now().date())
            else:
                typed, order = importer.csv_transactions(rows, date_order)
                summary = importer.import_transactions(db, typed, replace)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        db.commit()
        for key, count in summary.items():
            click.echo(f"{key}: {count}")
        if repeats:
            return
        click.echo(f"Dates read as {'/'.join(order)} (y = year, m = month, d = day).")
        click.echo("Imported per year; compare with the Income / Expenses totals on the yearly tabs:")
        for row in importer.imported_by_year(db):
            click.echo(f"  {row['year']}: {row['n']} entries ({row['first'][:10]} to {row['last'][:10]}), "
                       f"income {row['income'] / 100:.2f}, expenses {row['expense'] / 100:.2f}")

    @app.cli.command("ibkr-sync")
    @click.option("--days", type=int, help="Load this many days back (at most 365) instead of the query's period.")
    @click.option("--file", "path", type=click.Path(exists=True, dir_okay=False),
                  help="Read a Flex XML report saved from Client Portal instead of calling IBKR.")
    def ibkr_sync(days, path):
        """Fetch IBKR holdings now (the scheduler does this every morning)."""
        if path:
            with open(path, "rb") as handle:
                accounts = ibkr.parse_report(ET.fromstring(handle.read()))
            stored = ibkr.store(get_db(), accounts)
        else:
            if not ibkr.configured():
                raise click.ClickException("Set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID in .env first.")
            try:
                stored = ibkr.sync(days=days)
            except ibkr.FlexError as exc:
                raise click.ClickException(str(exc))
        for snap in holdings.latest(get_db()):
            click.echo(f"{snap['account']} on {snap['as_of']}: value {snap['total_cents'] / 100:,.2f} "
                       f"{snap['base_currency']}, cash {snap['cash_cents'] / 100:,.2f}, "
                       f"{len(snap['positions'])} positions")
        click.echo(f"Stored {stored} day(s).")

    @app.cli.command("set-webhook")
    def set_webhook():
        """Point the Telegram bot at this server."""
        cfg = app.config
        missing = [name for name in ("PUBLIC_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_PATH",
                                     "TELEGRAM_WEBHOOK_SECRET") if not cfg[name]]
        if missing:
            raise click.ClickException("Not set: " + ", ".join(missing))
        url = f"{cfg['PUBLIC_URL']}/telegram/{cfg['TELEGRAM_WEBHOOK_PATH']}"
        body = telegram.api("setWebhook", {
            "url": url,
            "secret_token": cfg["TELEGRAM_WEBHOOK_SECRET"],
            "allowed_updates": ["message", "callback_query"],
        }) or {}
        if not body.get("ok"):
            raise click.ClickException(f"Telegram refused: {body.get('description', 'no response')}")
        click.echo(f"Telegram now delivers updates to {url}")

    @app.cli.command("webhook-info")
    def webhook_info():
        """Where Telegram delivers updates, and its last delivery error."""
        body = telegram.api("getWebhookInfo") or {}
        for key, value in (body.get("result") or body).items():
            click.echo(f"{key}: {value}")

    @app.cli.command("run-repeats")
    def run_repeats():
        """Log repeat entries that are due (the scheduler does this every 10 minutes)."""
        created = repeats.run_due(get_db(), now().date())
        click.echo(f"Logged {len(created)} repeat entries.")

    @app.cli.command("backup")
    def backup():
        """Copy the database into BACKUP_DIR now."""
        click.echo(f"Wrote {maintenance.backup(now().date(), force=True)}")
