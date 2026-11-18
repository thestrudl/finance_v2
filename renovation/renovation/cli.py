"""Command-line helpers:

    docker compose exec renovation flask list-users
    docker compose exec renovation flask send-test-email you@example.com
    docker compose exec renovation flask check-finance
    docker compose exec renovation flask backup
"""
from datetime import timedelta

import click

from . import mail, maintenance, money
from .db import get_db
from .util import fmt_eur, now


def register(app):
    @app.cli.command("list-users")
    def list_users():
        """Who can log in (RENOVATION_USERS in .env, or PORTAL_USERS when that's empty)."""
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
        mail.deliver(mail.build(address, "Renovation test email",
                                f"SMTP works: sent through {cfg['SMTP_HOST']}:{cfg['SMTP_PORT']} as {cfg['SMTP_USERNAME']}."))
        click.echo(f"Sent to {address}.")

    @app.cli.command("check-finance")
    def check_finance():
        """Call the finance API once and show what came back."""
        body, problem = money.fetch_liquidity(now().date() + timedelta(days=90))
        if problem:
            raise click.ClickException(problem)
        balance = body["balance"]
        click.echo(f"Bank & cash: {fmt_eur(balance['amount_cents']) if balance else 'no balance check yet'}")
        click.echo(f"Security fund: {fmt_eur(body['security_fund_cents'])}")
        click.echo(f"Forecast: {len(body['forecast']['days'])} days to {body['forecast']['until']}")
        if "category_spend" in body:
            spend = body["category_spend"]
            click.echo(f"Category {spend['category']}: "
                       + (f"{fmt_eur(spend['total_cents'])} in total" if spend["found"] else "not found in finance"))

    @app.cli.command("backup")
    def backup():
        """Copy the database into BACKUP_DIR now."""
        click.echo(f"Wrote {maintenance.backup(now().date(), force=True)}")
