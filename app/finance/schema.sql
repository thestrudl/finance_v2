-- Money is stored in integer cents. Times are local (APP_TIMEZONE) and stored
-- as 'YYYY-MM-DD HH:MM:SS' so string comparison orders them correctly.

CREATE TABLE IF NOT EXISTS categories (
    key           TEXT PRIMARY KEY,                -- canonical spelling, lowercase
    label         TEXT NOT NULL,
    grp           TEXT NOT NULL DEFAULT 'living'
                  CHECK (grp IN ('living', 'business', 'project', 'savings')),
    budget_cents  INTEGER,
    budget_period TEXT CHECK (budget_period IN ('month', 'year')),
    description   TEXT NOT NULL DEFAULT ''         -- notes shown on the category page
);

-- Other spellings that should count as an existing category ("clotes" -> clothing).
CREATE TABLE IF NOT EXISTS category_aliases (
    alias        TEXT PRIMARY KEY,
    category_key TEXT NOT NULL REFERENCES categories(key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS repeats (
    id           INTEGER PRIMARY KEY,
    user_name    TEXT,
    amount_cents INTEGER NOT NULL,
    category     TEXT NOT NULL,                    -- as typed, resolved when logged
    comment      TEXT NOT NULL DEFAULT '',
    is_income    INTEGER NOT NULL,
    day_of_month INTEGER NOT NULL CHECK (day_of_month BETWEEN 1 AND 31),
    active_from  TEXT NOT NULL,                    -- first date it may be logged
    created_at   TEXT NOT NULL
);

-- One row per repeat per due date, kept even if the logged transaction is
-- later deleted, so a deleted entry is not logged again.
CREATE TABLE IF NOT EXISTS repeat_runs (
    repeat_id INTEGER NOT NULL REFERENCES repeats(id) ON DELETE CASCADE,
    due_date  TEXT NOT NULL,
    PRIMARY KEY (repeat_id, due_date)
);

CREATE TABLE IF NOT EXISTS transactions (
    id               INTEGER PRIMARY KEY,
    occurred_at      TEXT NOT NULL,
    user_name        TEXT,
    telegram_user_id INTEGER,
    amount_cents     INTEGER NOT NULL,             -- negative expense = refund
    category         TEXT NOT NULL REFERENCES categories(key),
    category_raw     TEXT NOT NULL,                -- as typed
    comment          TEXT NOT NULL DEFAULT '',
    is_income        INTEGER NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('telegram', 'repeat', 'import', 'web')),
    repeat_id        INTEGER REFERENCES repeats(id) ON DELETE SET NULL,
    off_balance      INTEGER NOT NULL DEFAULT 0,   -- paid or received outside our accounts (a gift or loan paid directly)
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transactions_occurred_at ON transactions (occurred_at);
CREATE INDEX IF NOT EXISTS transactions_category ON transactions (category, occurred_at);

-- Telegram retries a delivery it thinks failed; remembering update ids keeps
-- a retry from logging the same expense twice.
CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id   INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL
);

-- Telegram entries whose category looked like a typo, waiting for the
-- "Did you mean ...?" buttons. Nothing is stored until someone answers.
CREATE TABLE IF NOT EXISTS pending_entries (
    id               INTEGER PRIMARY KEY,
    chat_id          INTEGER NOT NULL,
    message_id       INTEGER,                      -- the question, edited into the answer
    telegram_user_id INTEGER,
    user_name        TEXT,
    kind             TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'repeat')),
    category_raw     TEXT NOT NULL,
    suggestion       TEXT NOT NULL,                -- category key offered instead
    amount_text      TEXT NOT NULL,                -- as typed, echoed back
    comment          TEXT NOT NULL DEFAULT '',
    is_income        INTEGER NOT NULL,
    day_of_month     INTEGER,
    created_at       TEXT NOT NULL,
    resolved_at      TEXT
);

-- What was really in the bank accounts and cash, all together, at a moment.
-- The estimate of today's liquidity is the latest check plus income minus
-- spending logged since; the next check shows how far that drifted.
CREATE TABLE IF NOT EXISTS balance_checks (
    id           INTEGER PRIMARY KEY,
    checked_at   TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'web',      -- web | telegram
    user_name    TEXT,
    created_at   TEXT NOT NULL
);

-- Small household settings, e.g. security_fund_cents: money kept aside that
-- doesn't count as free to spend.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Holdings: an account's value at the close of a day, as its source reports it
-- (IBKR Flex for now). Money in the account's base currency, in cents.
CREATE TABLE IF NOT EXISTS holding_snapshots (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,                   -- 'ibkr'
    account       TEXT NOT NULL,                   -- e.g. U1234567
    as_of         TEXT NOT NULL,                   -- YYYY-MM-DD, the report date
    base_currency TEXT NOT NULL,
    cash_cents    INTEGER NOT NULL DEFAULT 0,
    stock_cents   INTEGER NOT NULL DEFAULT 0,      -- stocks and ETFs
    funds_cents   INTEGER NOT NULL DEFAULT 0,
    bonds_cents   INTEGER NOT NULL DEFAULT 0,
    options_cents INTEGER NOT NULL DEFAULT 0,
    other_cents   INTEGER NOT NULL DEFAULT 0,      -- accruals and anything else in the total
    total_cents   INTEGER NOT NULL,                -- net asset value
    fetched_at    TEXT NOT NULL,
    UNIQUE (source, account, as_of)
);

-- Positions and cash per currency, kept for the latest report of each account.
CREATE TABLE IF NOT EXISTS holding_positions (
    snapshot_id           INTEGER NOT NULL REFERENCES holding_snapshots(id) ON DELETE CASCADE,
    asset_class           TEXT NOT NULL,           -- STK (incl. ETFs), FUND, BOND, OPT, ...
    symbol                TEXT NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    isin                  TEXT,
    currency              TEXT NOT NULL,
    quantity              REAL NOT NULL,
    price                 REAL,                    -- mark price, position currency
    value_cents           INTEGER NOT NULL,        -- position currency
    value_base_cents      INTEGER NOT NULL,
    cost_base_cents       INTEGER,
    unrealized_base_cents INTEGER,
    percent_of_nav        REAL
);

CREATE TABLE IF NOT EXISTS holding_cash (
    snapshot_id   INTEGER NOT NULL REFERENCES holding_snapshots(id) ON DELETE CASCADE,
    currency      TEXT NOT NULL,
    cash_cents    INTEGER NOT NULL,                -- in that currency
    settled_cents INTEGER,
    base_cents    INTEGER                          -- converted, when a rate was reported
);

-- When each automatic sync last ran, worked or failed.
CREATE TABLE IF NOT EXISTS sync_status (
    name         TEXT PRIMARY KEY,                 -- 'ibkr'
    last_attempt TEXT,
    last_success TEXT,
    last_error   TEXT,
    alerted_on   TEXT                              -- date the admin was last told about an error
);

-- Portal accounts, kept in sync with PORTAL_USERS in .env. No passwords:
-- logging in means proving you can read the account's email.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- One emailed login: a 6-digit code and a link, single-use and short-lived.
-- Only hashes are stored; the code hash is keyed with SECRET_KEY.
CREATE TABLE IF NOT EXISTS login_tokens (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash  TEXT NOT NULL,
    link_hash  TEXT NOT NULL UNIQUE,
    attempts   INTEGER NOT NULL DEFAULT 0,
    ip         TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

-- Login emails requested and wrong codes entered, per IP, for throttling.
CREATE TABLE IF NOT EXISTS login_events (
    ip   TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('request', 'failure')),
    at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS login_events_ip ON login_events (ip, kind, at);
