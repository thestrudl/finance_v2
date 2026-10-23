# finance

Self-hosted household finance. You log expenses and income by sending short messages to a Telegram bot (`food 5.5 lunch`, `in salary 2500`). A small Flask app on your own server stores them in SQLite and serves a private web portal with monthly, yearly, living-cost, business and holdings dashboards.

It started as a Google Apps Script bot writing to a spreadsheet. The message format is the same, so moving over changes nothing for whoever logs the entries.

```
Telegram ──▶ https://finance.example.com/telegram/<secret path>   webhook, checked with a secret header
Browser  ──▶ https://finance.example.com/                          one-time code by email → dashboards
finance-scheduler container: monthly repeats, daily backup, morning IBKR sync
```

## What it does

**Logging, through Telegram**
- `food 5.5 lunch` logs an expense, `in salary 2500` logs income, and `repeat tel 19.89 18` adds a monthly entry on day 18.
- `report` sends this month as a chart. `summary` gives last week or last month in numbers: against usual, budgets, businesses and unusual expenses.
- `balance` gives the estimated bank and cash; `balance 4520.30` records the real total. `undo` removes your last entry, and `help` lists everything.
- Typo check: if a new category is one or two letters off one you use regularly (`fodo`), the bot asks "Did you mean Food?" with buttons, and stores nothing until someone answers.

**Portal**
- **Overview (a month):**
  - Totals against a typical month (the median of the 12 before) and the same month last year.
  - Living costs day by day, with a projection for the month in progress.
  - Categories, budgets, unusual expenses (3× a category's usual amount or more) and the largest expenses.
- **Year:**
  - Totals against last year up to the same date.
  - Income and spending by month, and the running net.
  - Where the money went and where it came from, by group.
  - The biggest changes by category, and a category-by-month heatmap.
- **Living:** everyday costs only, with a small chart per category.
- **Business:** a page per business: profit and loss by month, seasonality against last year, and profit since the start (how much of the investment has been earned back).
- **Holdings:**
  - **Bank and cash, estimated:** the last balance check plus everything logged since, with the drift shown at each new check.
  - **Interactive Brokers:** account value, cash, allocation and positions, from IBKR's read-only Flex Web Service.
  - **Net worth:** the two together.
- **Data:** edit transactions, merge misspelt categories (every entry keeps its original spelling, and Split off undoes a merge), set groups and budgets, and manage repeats.

Groups decide what counts where: *Living*, *Business*, *Project* (a house, a wedding) and *Savings*. "Without projects & savings" hides one-off amounts so they don't swamp everyday spending.

**Built for privacy**
- **Login:** passwordless. Only addresses listed in `.env` can log in, with a 6-digit code or a link sent over SMTPS. Codes are single-use, short-lived and rate-limited, and only their hashes are stored.
- **No third parties:** fonts and Chart.js are served from the app itself, so the portal makes no requests to anyone else. A strict content security policy, CSRF protection and secure cookies.
- **Your data stays home:** everything lives in one SQLite file on your server, with daily backups.

## Stack

Python 3.12, Flask, gunicorn and SQLite, with Chart.js (vendored) and matplotlib for Telegram charts. It runs in Docker Compose behind Traefik, with Let's Encrypt certificates. Dark theme only. It uses the typefaces of [blog.rokvidmar.xyz](https://blog.rokvidmar.xyz/): Inter, Space Grotesk and IBM Plex Mono.

| Path | What |
|---|---|
| `app/finance/bot.py`, `parser.py` | Telegram webhook and message grammar |
| `app/finance/auth.py`, `mail.py` | Passwordless login, SMTPS |
| `app/finance/dashboard.py`, `insights.py` | Portal pages and the queries behind them |
| `app/finance/liquidity.py`, `ibkr.py`, `holdings.py` | Bank and cash estimate, IBKR Flex, holdings |
| `app/finance/reports.py` | Telegram `report` chart and `summary` |
| `app/finance/repeats.py`, `scheduler.py` | Monthly repeats, backups, morning sync |
| `app/finance/importer.py` | Import from a Google Sheet (.csv or .ods) |
| `app/household.example.json` | Example household categories, spellings and merges |

## Setup

Requirements: a server with Docker, a domain, Traefik on an external `nginx-proxy` network with a `letsencrypt` certificate resolver (or adapt the labels in `docker-compose.yml`), a Telegram bot from @BotFather, and an SMTP mailbox.

1. Clone the repository, copy `.env.example` to `.env`, and fill it in. Generate random values with:
   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   `SECRET_KEY`, `TELEGRAM_WEBHOOK_PATH` and `TELEGRAM_WEBHOOK_SECRET` each need their own.
2. Optional: copy `app/household.example.json` to `app/household.json` and add your own categories. It's git-ignored. This file holds:
   - categories with their group and notes,
   - extra spellings (`"pharmacy": "health"`),
   - one-off merges for imported data.
3. Start the containers and check the setup:
   ```sh
   docker compose up -d --build
   docker compose exec finance flask send-test-email you@example.com
   docker compose exec finance flask list-users
   docker compose exec finance flask set-webhook
   ```
   `set-webhook` tells Telegram to deliver messages to `https://FINANCE_HOST/telegram/<TELEGRAM_WEBHOOK_PATH>`, with the secret header.
4. Message the bot from your group. If it answers that the chat isn't set up, add the chat id from its reply to `TELEGRAM_ALLOWED_IDS` and run `docker compose up -d`.

### Importing from a Google Sheet

Export the tab with the entries as CSV (`File > Download > CSV`; only the open tab is saved), copy it to the server, and run:

```sh
docker compose exec -T finance flask import-csv - < entries.csv              # date, user, amount, category, comment, income
docker compose exec -T finance flask import-csv --repeats - < repeats.csv    # ... plus day of month
```

How it reads the file:
- **Dates:** US and European formats both work; the date order is worked out from the column.
- **Amounts and booleans:** decimal commas and localized TRUE/FALSE work.
- **Reversed mistakes:** a wrong entry followed straight away by its exact negative is dropped as a pair.
- **Whole workbook:** `flask import-ods` reads it, including budgets.

### Interactive Brokers (optional)

1. In Client Portal, go to **Performance & Reports → Flex Queries** and create an **Activity Flex Query**.
   - Sections: *Account Information*, *Net Asset Value (NAV) in Base*, *Open Positions* (Summary), *Cash Report*, and optionally *Conversion Rates*.
   - Format **XML**, period **Last Business Day**, date format **yyyyMMdd**.
2. Under **Flex Web Service**, generate a token (valid for up to a year, ideally limited to your server's IP address).
3. Set `IBKR_FLEX_TOKEN` and `IBKR_FLEX_QUERY_ID` in `.env`, then run:
   ```sh
   docker compose up -d && docker compose exec finance flask ibkr-sync --days 365
   ```

Values are as of the last market close. Margin figures aren't part of Flex reports. When the token expires, the admin gets a Telegram message.

## Backups

The scheduler writes `/data/backups/finance-YYYY-MM-DD.db` daily and keeps 14 copies. They sit on the same disk, so copy one elsewhere now and then:

```sh
docker compose cp finance:/data/backups/finance-$(date +%F).db .
```

## Tests

```sh
cd app && pip install -r requirements-dev.txt && pytest
```

## License

[MIT](LICENSE) © Rok Vidmar ([blog.rokvidmar.xyz](https://blog.rokvidmar.xyz/)). Free to use, change and sell, including commercially. Keep the copyright notice and license text in copies or substantial parts of the code.

Third-party files keep their own licenses:
- **Chart.js:** MIT, notice in `app/finance/static/vendor/chart.umd.js`.
- **Inter, Space Grotesk, IBM Plex Mono:** SIL Open Font License 1.1, texts in `app/finance/static/fonts/`.
