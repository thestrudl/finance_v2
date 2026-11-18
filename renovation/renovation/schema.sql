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
    paid_cents  INTEGER NOT NULL DEFAULT 0,        -- paid so far (a deposit, or all of it)
    pay_at      TEXT NOT NULL DEFAULT 'start' CHECK (pay_at IN ('start', 'end')),
    who         TEXT NOT NULL DEFAULT '',          -- us, or the contractor
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_goal ON tasks (goal_id);

-- task_id can't start before needs_id is finished.
CREATE TABLE IF NOT EXISTS task_deps (
    task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    needs_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, needs_id),
    CHECK (task_id <> needs_id)
);
CREATE INDEX IF NOT EXISTS task_deps_needs ON task_deps (needs_id);

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
