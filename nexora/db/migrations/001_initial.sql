-- Nexora initial schema. SQLite dialect kept close to standard SQL so a
-- PostgreSQL port only needs AUTOINCREMENT -> SERIAL style changes.

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id    INTEGER NOT NULL,
    module      TEXT    NOT NULL,
    data        TEXT    NOT NULL DEFAULT '{}',
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, module)
);

CREATE TABLE IF NOT EXISTS guilds (
    guild_id     INTEGER PRIMARY KEY,
    name         TEXT,
    icon         TEXT,
    owner_id     INTEGER,
    member_count INTEGER DEFAULT 0,
    joined_at    TEXT DEFAULT (datetime('now')),
    active       INTEGER DEFAULT 1
);

-- ---------------------------------------------------------------- logging
CREATE TABLE IF NOT EXISTS log_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    category     TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    user_id      INTEGER,
    moderator_id INTEGER,
    channel_id   INTEGER,
    summary      TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_log_guild_time ON log_events (guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_log_guild_cat ON log_events (guild_id, category);
CREATE INDEX IF NOT EXISTS idx_log_guild_user ON log_events (guild_id, user_id);

-- ------------------------------------------------------------- moderation
CREATE TABLE IF NOT EXISTS mod_cases (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    case_number      INTEGER NOT NULL,
    action           TEXT NOT NULL,
    user_id          INTEGER NOT NULL,
    moderator_id     INTEGER,
    reason           TEXT,
    duration_seconds INTEGER,
    expires_at       TEXT,
    active           INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (guild_id, case_number)
);
CREATE INDEX IF NOT EXISTS idx_cases_user ON mod_cases (guild_id, user_id);

CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER,
    reason       TEXT,
    case_number  INTEGER,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_warn_user ON warnings (guild_id, user_id, active);

CREATE TABLE IF NOT EXISTS mod_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER,
    note         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS appeals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    case_number  INTEGER,
    content      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    reviewer_id  INTEGER,
    response     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at  TEXT
);

CREATE TABLE IF NOT EXISTS automod_flags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    channel_id   INTEGER,
    rule         TEXT NOT NULL,
    content      TEXT,
    action_taken TEXT,
    case_number  INTEGER,
    status       TEXT NOT NULL DEFAULT 'pending',
    reviewer_id  INTEGER,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_flags_guild ON automod_flags (guild_id, status);

-- ---------------------------------------------------------------- tickets
CREATE TABLE IF NOT EXISTS ticket_panels (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    name              TEXT NOT NULL,
    channel_id        INTEGER,
    message_id        INTEGER,
    category_id       INTEGER,
    staff_role_ids    TEXT NOT NULL DEFAULT '[]',
    title             TEXT NOT NULL DEFAULT 'Support',
    description       TEXT NOT NULL DEFAULT 'Click the button below to open a ticket.',
    button_label      TEXT NOT NULL DEFAULT 'Open Ticket',
    button_emoji      TEXT,
    color             INTEGER,
    questions         TEXT NOT NULL DEFAULT '[]',
    max_open_per_user INTEGER NOT NULL DEFAULT 1,
    welcome_message   TEXT NOT NULL DEFAULT 'Hello {user}! Staff will be with you shortly.',
    name_format       TEXT NOT NULL DEFAULT 'ticket-{number}',
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    panel_id     INTEGER,
    number       INTEGER NOT NULL,
    channel_id   INTEGER,
    opener_id    INTEGER NOT NULL,
    claimed_by   INTEGER,
    status       TEXT NOT NULL DEFAULT 'open',
    priority     TEXT NOT NULL DEFAULT 'normal',
    answers      TEXT NOT NULL DEFAULT '[]',
    rating       INTEGER,
    rating_comment TEXT,
    close_reason TEXT,
    closed_by    INTEGER,
    transcript   TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets (channel_id);

-- ----------------------------------------------------------- applications
CREATE TABLE IF NOT EXISTS application_forms (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    name              TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'custom',
    description       TEXT NOT NULL DEFAULT '',
    questions         TEXT NOT NULL DEFAULT '[]',
    review_channel_id INTEGER,
    accept_role_ids   TEXT NOT NULL DEFAULT '[]',
    remove_role_ids   TEXT NOT NULL DEFAULT '[]',
    reviewer_role_ids TEXT NOT NULL DEFAULT '[]',
    cooldown_hours    INTEGER NOT NULL DEFAULT 24,
    max_submissions   INTEGER NOT NULL DEFAULT 0,
    accept_message    TEXT NOT NULL DEFAULT 'Congratulations! Your {form} application in {server} was accepted.',
    deny_message      TEXT NOT NULL DEFAULT 'Thank you for applying. Your {form} application in {server} was not accepted this time.',
    required_votes    INTEGER NOT NULL DEFAULT 1,
    panel_channel_id  INTEGER,
    panel_message_id  INTEGER,
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS application_submissions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    form_id           INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    answers           TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'pending',
    reviewer_id       INTEGER,
    decision_reason   TEXT,
    review_message_id INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_subs_guild ON application_submissions (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_subs_user ON application_submissions (guild_id, form_id, user_id);

CREATE TABLE IF NOT EXISTS application_votes (
    submission_id INTEGER NOT NULL,
    reviewer_id   INTEGER NOT NULL,
    vote          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (submission_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS application_notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    reviewer_id   INTEGER NOT NULL,
    note          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- -------------------------------------------------------------- giveaways
CREATE TABLE IF NOT EXISTS giveaways (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id             INTEGER NOT NULL,
    channel_id           INTEGER NOT NULL,
    message_id           INTEGER,
    host_id              INTEGER,
    prize                TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    winner_count         INTEGER NOT NULL DEFAULT 1,
    starts_at            TEXT NOT NULL,
    ends_at              TEXT NOT NULL,
    required_role_ids    TEXT NOT NULL DEFAULT '[]',
    bonus_roles          TEXT NOT NULL DEFAULT '{}',
    min_account_age_days INTEGER NOT NULL DEFAULT 0,
    min_level            INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'scheduled',
    winners              TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_giveaways_status ON giveaways (status, ends_at);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    entries     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (giveaway_id, user_id)
);

-- -------------------------------------------------------------- community
CREATE TABLE IF NOT EXISTS levels (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    xp          INTEGER NOT NULL DEFAULT 0,
    level       INTEGER NOT NULL DEFAULT 0,
    messages    INTEGER NOT NULL DEFAULT 0,
    last_xp_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS economy (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    balance     INTEGER NOT NULL DEFAULT 0,
    total_earned INTEGER NOT NULL DEFAULT 0,
    last_daily  REAL NOT NULL DEFAULT 0,
    last_weekly REAL NOT NULL DEFAULT 0,
    last_work   REAL NOT NULL DEFAULT 0,
    daily_streak INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS shop_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price       INTEGER NOT NULL,
    role_id     INTEGER,
    stock       INTEGER NOT NULL DEFAULT -1,
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    item_id  INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, item_id)
);

CREATE TABLE IF NOT EXISTS achievements (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    key        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, user_id, key)
);

CREATE TABLE IF NOT EXISTS suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    number      INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    content     TEXT NOT NULL,
    channel_id  INTEGER,
    message_id  INTEGER,
    status      TEXT NOT NULL DEFAULT 'open',
    response    TEXT,
    responder_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS suggestion_votes (
    suggestion_id INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    vote          INTEGER NOT NULL,
    PRIMARY KEY (suggestion_id, user_id)
);

CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    author_id  INTEGER,
    question   TEXT NOT NULL,
    options    TEXT NOT NULL,
    multi      INTEGER NOT NULL DEFAULT 0,
    ends_at    TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    option_index INTEGER NOT NULL,
    PRIMARY KEY (poll_id, user_id, option_index)
);

CREATE TABLE IF NOT EXISTS role_menus (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER,
    message_id  INTEGER,
    kind        TEXT NOT NULL DEFAULT 'button',
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    options     TEXT NOT NULL DEFAULT '[]',
    max_values  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS temp_roles (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    role_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, role_id)
);

CREATE TABLE IF NOT EXISTS custom_commands (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    response   TEXT NOT NULL DEFAULT '',
    embed      TEXT,
    delete_trigger INTEGER NOT NULL DEFAULT 0,
    required_role_ids TEXT NOT NULL DEFAULT '[]',
    enabled    INTEGER NOT NULL DEFAULT 1,
    uses       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS automations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    conditions  TEXT NOT NULL DEFAULT '[]',
    actions     TEXT NOT NULL DEFAULT '[]',
    enabled     INTEGER NOT NULL DEFAULT 1,
    runs        INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS automation_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    automation_id INTEGER NOT NULL,
    guild_id      INTEGER NOT NULL,
    status        TEXT NOT NULL,
    detail        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    guild_id   INTEGER,
    channel_id INTEGER,
    content    TEXT NOT NULL,
    remind_at  TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS birthdays (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    month    INTEGER NOT NULL,
    day      INTEGER NOT NULL,
    last_announced_year INTEGER,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    embed        TEXT,
    next_run_at  TEXT NOT NULL,
    repeat_hours INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   INTEGER,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS starboard_posts (
    guild_id        INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    star_message_id INTEGER,
    stars           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, message_id)
);

CREATE TABLE IF NOT EXISTS counting_state (
    guild_id     INTEGER PRIMARY KEY,
    current      INTEGER NOT NULL DEFAULT 0,
    last_user_id INTEGER,
    high_score   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS saved_embeds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------------- analytics
CREATE TABLE IF NOT EXISTS stats_daily (
    guild_id      INTEGER NOT NULL,
    day           TEXT NOT NULL,
    joins         INTEGER NOT NULL DEFAULT 0,
    leaves        INTEGER NOT NULL DEFAULT 0,
    messages      INTEGER NOT NULL DEFAULT 0,
    voice_minutes INTEGER NOT NULL DEFAULT 0,
    mod_actions   INTEGER NOT NULL DEFAULT 0,
    member_count  INTEGER,
    PRIMARY KEY (guild_id, day)
);

CREATE TABLE IF NOT EXISTS staff_activity (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    day       TEXT NOT NULL,
    actions   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, day)
);

CREATE TABLE IF NOT EXISTS feature_usage (
    guild_id INTEGER NOT NULL,
    feature  TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, feature)
);

-- ----------------------------------------------------------- web / system
CREATE TABLE IF NOT EXISTS web_sessions (
    id           TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    user_json    TEXT NOT NULL,
    access_token TEXT NOT NULL,
    guilds_json  TEXT,
    guilds_fetched_at REAL,
    csrf_token   TEXT NOT NULL,
    expires_at   REAL NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dashboard_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS error_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    guild_id   INTEGER,
    message    TEXT NOT NULL,
    traceback  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
