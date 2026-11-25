"""Server commands, run inside the container:

    docker compose exec finance flask send-test-email you@example.com
    docker compose exec -T finance flask import-csv - < webHookData.csv
    docker compose exec -T finance flask import-csv --repeats - < repeatData.csv
    docker compose exec finance flask set-webhook
    docker compose exec -T finance flask check-statement - < promet.csv
"""
import io
import sys
import xml.etree.ElementTree as ET

import click

from . import categories, holdings, ibkr, importer, mail, maintenance, repeats, statement, telegram
from .db import get_db
from .util import fmt_month, now


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
        if cfg["RESTORE_MODE"]:
            click.echo(f"RESTORE_MODE: sent to {cfg['RESTORE_MAIL_TO']} instead." if cfg["RESTORE_MAIL_TO"]
                       else "RESTORE_MODE: not sent, written to the log only.")
        else:
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
    @click.option("--append", is_flag=True,
                  help="Add the rows to what is there, as entered by hand (e.g. from check-statement --csv).")
    @click.option("--date-order", type=click.Choice(["mdy", "dmy"]),
                  help="Only needed if the dates can't tell (5/4/2024 could be either).")
    def import_csv(path, repeats, replace, append, date_order):
        """Import one tab of the Google Sheet saved as .csv. PATH '-' reads standard input."""
        data = sys.stdin.buffer.read() if path == "-" else open(path, "rb").read()
        rows = importer.read_csv(data)
        db = get_db()
        try:
            if repeats:
                summary = importer.import_repeats(db, importer.csv_repeats(rows), now().date())
            else:
                typed, order = importer.csv_transactions(rows, date_order)
                if append and any(not (row[3] or "").strip() for row in typed):
                    raise click.ClickException("Every row needs a category; fill in the empty ones first.")
                summary = importer.import_transactions(db, typed, replace, append)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        db.commit()
        for key, count in summary.items():
            click.echo(f"{key}: {count}")
        if repeats or append:
            return
        click.echo(f"Dates read as {'/'.join(order)} (y = year, m = month, d = day).")
        click.echo("Imported per year; compare with the Income / Expenses totals on the yearly tabs:")
        for row in importer.imported_by_year(db):
            click.echo(f"  {row['year']}: {row['n']} entries ({row['first'][:10]} to {row['last'][:10]}), "
                       f"income {row['income'] / 100:.2f}, expenses {row['expense'] / 100:.2f}")

    @app.cli.command("check-statement")
    @click.argument("path")
    @click.option("--all", "show_all", is_flag=True, help="Also list the lines that are logged or skipped.")
    @click.option("--csv", "csv_path",
                  help="Write the missing lines to this file ('-': print only them), ready for import-csv --append.")
    def check_statement(path, show_all, csv_path):
        """Find bank statement payments that were never logged (NLB CSV export). PATH '-' reads standard input."""
        data = sys.stdin.buffer.read() if path == "-" else open(path, "rb").read()
        try:
            lines = statement.read_nlb(data)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        household = categories.load_household(app.config.get("HOUSEHOLD_FILE"))
        results = statement.compare(get_db(), statement.apply_rules(lines, household["statement"]))
        if csv_path == "-":
            click.echo(statement.missing_csv(results), nl=False)
            return
        marks = {"logged": "ok   ", "close": "~    ", "split": "part ", "combined": "sum  ",
                 "rounded_sum": "sum~ ", "reversed": "SIGN ", "skipped": "skip ", "missing": "MISS "}
        month = None
        for result in results:
            if result.status in ("logged", "skipped") and not show_all:
                continue
            line = result.line
            if line.day.strftime("%Y-%m") != month:
                month = line.day.strftime("%Y-%m")
                click.echo(f"\n{fmt_month(month)}")
            amount = f"{'+' if line.is_income else ''}{line.amount / 100:,.2f}"
            note = ""
            if result.entries:
                note = "  = " + " + ".join(
                    f"{e['label']} {e['amount_cents'] / 100:.2f} {e['comment']} ({e['day']:%d.%m.})".replace("  ", " ")
                    for e in result.entries)
                if result.status == "split":
                    note += ", paid in parts"
                elif result.status == "reversed":
                    note += ": logged with the wrong sign, fix that entry"
            elif result.status == "skipped":
                note = f"  ({line.rule['skip']})"
            elif line.rule.get("category"):
                note = f"  -> {line.rule['category']}"
            click.echo(f"  {marks[result.status]}{line.day:%d.%m.} {amount:>10}  {line.text[:60]}{note}")
        counts = {status: sum(r.status == status for r in results) for status in marks}
        click.echo(f"\n{counts['missing']} missing, {counts['reversed']} with the wrong sign, "
                   f"{counts['close'] + counts['rounded_sum']} with a different amount, "
                   f"{counts['split'] + counts['combined']} adding up to other entries, "
                   f"{counts['logged']} logged, {counts['skipped']} skipped.")
        if csv_path:
            with open(csv_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(statement.missing_csv(results))
            click.echo(f"Missing lines written to {csv_path}. Fill in the categories, then: "
                       "flask import-csv --append FILE")

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
