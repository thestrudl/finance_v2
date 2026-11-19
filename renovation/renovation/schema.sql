-- Money is stored in integer cents, days as 'YYYY-MM-DD', times as local
-- (APP_TIMEZONE) 'YYYY-MM-DD HH:MM:SS'.

-- A big item: "Doors on the 2nd floor", finished by a deadline.
CREATE TABLE IF NOT EXISTS goals (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    deadline     TEXT NOT NULL,                    -- everything under it must be finished by this day
    budget_cents INTEGER,                          -- rough estimate, used until it has tasks with costs
    notes        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

-- The work under a goal. Its dates are worked out, not stored: it starts as
-- soon as everything it needs is finished (and not before `not_before`), and
-- takes `days` calendar days.
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    goal_id     INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    days        INTEGER NOT NULL DEFAULT 1 CHECK (days BETWEEN 1 AND 3650),
    not_before  TEXT,                              -- earliest start, e.g. the contractor's date
    status      TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'doing', 'done')),
    started_on  TEXT,                              -- set when it starts
    done_on     TEXT,                              -- set when it's finished
    cost_cents  INTEGER NOT NULL DEFAULT 0,        -- estimated
    pay_at      TEXT NOT NULL DEFAULT 'start' CHECK (pay_at IN ('start', 'end')),
    who         TEXT NOT NULL DEFAULT '',          -- us, or the contractor
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_goal ON tasks (goal_id);

-- Money agreed but not paid yet: a signed offer, an order. Under a big item,
-- optionally for one of its tasks. Counts in the cash flow as firm.
CREATE TABLE IF NOT EXISTS commitments (
    id           INTEGER PRIMARY KEY,
    goal_id      INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    task_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    what         TEXT NOT NULL,
    who          TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    due_on       TEXT,                             -- empty: when the task is paid for, else the deadline
    notes        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS commitments_goal ON commitments (goal_id);

-- Money that went out. Logged in finance as an expense (finance = 'logged')
-- under finance_key, unless it was there already ('skip'); 'failed' means
-- finance didn't answer and it still has to be sent; 'linked' is a payment
-- logged in finance some other way, picked from its list (finance_tx_id).
CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY,
    goal_id       INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    commitment_id INTEGER REFERENCES commitments(id) ON DELETE SET NULL,
    paid_on       TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents <> 0),  -- negative: a refund
    what          TEXT NOT NULL DEFAULT '',
    off_balance   INTEGER NOT NULL DEFAULT 0,      -- paid outside our accounts (a loan paid to the seller)
    finance       TEXT NOT NULL DEFAULT 'skip' CHECK (finance IN ('logged', 'failed', 'skip', 'linked')),
    finance_key   TEXT UNIQUE,
    finance_tx_id INTEGER UNIQUE,                  -- 'linked': the finance transaction it is
    finance_error TEXT,
    user_name     TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS payments_goal ON payments (goal_id);

-- Waiting that involves a whole big item: a big item (all its tasks) or a
-- task waits for a big item (all its tasks finished) or for one task.
-- Task waiting for task is task_deps.
CREATE TABLE IF NOT EXISTS item_deps (
    id            INTEGER PRIMARY KEY,
    goal_id       INTEGER REFERENCES goals(id) ON DELETE CASCADE,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    needs_goal_id INTEGER REFERENCES goals(id) ON DELETE CASCADE,
    needs_task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    CHECK ((goal_id IS NULL) <> (task_id IS NULL)),
    CHECK ((needs_goal_id IS NULL) <> (needs_task_id IS NULL)),
    CHECK (task_id IS NULL OR needs_task_id IS NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS item_deps_unique ON item_deps
    (COALESCE(goal_id, 0), COALESCE(task_id, 0), COALESCE(needs_goal_id, 0), COALESCE(needs_task_id, 0));

-- task_id can't start before needs_id is finished.
CREATE TABLE IF NOT EXISTS task_deps (
    task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    needs_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, needs_id),
    CHECK (task_id <> needs_id)
);
CREATE INDEX IF NOT EXISTS task_deps_needs ON task_deps (needs_id);

-- Each scheduled "ready to start" email, once sent ('YYYY-MM-DD HH:MM:SS' of its slot).
CREATE TABLE IF NOT EXISTS digest_runs (
    slot    TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

-- Logins: the same passwordless scheme as the finance portal, kept in sync
-- with RENOVATION_USERS (or PORTAL_USERS) from .env.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

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

CREATE TABLE IF NOT EXISTS login_events (
    ip   TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('request', 'failure')),
    at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS login_events_ip ON login_events (ip, kind, at);
