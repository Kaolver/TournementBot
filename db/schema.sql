-- SQLite schema for TournamentBot.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS guilds (
    guild_id              INTEGER PRIMARY KEY,
    tournament_channel_id INTEGER,
    to_role_id            INTEGER,
    timezone              TEXT    NOT NULL DEFAULT 'UTC',
    deadline_hours        INTEGER NOT NULL DEFAULT 24,
    auto_sync             INTEGER NOT NULL DEFAULT 1,
    syncs_per_day         INTEGER NOT NULL DEFAULT 12,
    event_channel_id      INTEGER,
    event_location        TEXT,
    event_duration        INTEGER NOT NULL DEFAULT 60,
    first_match_day       TEXT,
    days_per_round        INTEGER NOT NULL DEFAULT 0,
    round_days            TEXT
);

CREATE TABLE IF NOT EXISTS tournaments (
    challonge_id         INTEGER PRIMARY KEY,
    guild_id             INTEGER NOT NULL,
    name                 TEXT    NOT NULL,
    url                  TEXT,
    full_url             TEXT,
    tournament_type      TEXT,
    state                TEXT    NOT NULL DEFAULT 'pending',
    channel_id           INTEGER,
    signup_message_id    INTEGER,
    bracket_message_id   INTEGER,
    archived             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    next_refresh_at      TEXT,
    last_refresh_at      TEXT,
    refresh_window_start TEXT,
    refresh_window_count INTEGER NOT NULL DEFAULT 0,
    sync_day             TEXT,
    sync_day_count       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS participants (
    tournament_id   INTEGER NOT NULL,
    participant_id  INTEGER NOT NULL,
    discord_user_id INTEGER,
    name            TEXT    NOT NULL,
    seed            INTEGER,
    final_rank      INTEGER,
    PRIMARY KEY (tournament_id, participant_id)
);

CREATE TABLE IF NOT EXISTS signups (
    tournament_id   INTEGER NOT NULL,
    discord_user_id INTEGER NOT NULL,
    name            TEXT    NOT NULL,
    seed            INTEGER,
    signed_up_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tournament_id, discord_user_id)
);

CREATE TABLE IF NOT EXISTS matches (
    tournament_id         INTEGER NOT NULL,
    match_id              INTEGER NOT NULL,
    identifier            TEXT,
    round                 INTEGER NOT NULL DEFAULT 0,
    play_order            INTEGER,
    player1_id            INTEGER,
    player2_id            INTEGER,
    state                 TEXT    NOT NULL DEFAULT 'pending',
    winner_id             INTEGER,
    scores                TEXT,
    thread_id             INTEGER,
    scheduled_at          TEXT,
    agreed_at             TEXT,
    deadline_at           TEXT,
    play_by               TEXT,
    scheduling_status     TEXT    NOT NULL DEFAULT 'pending',
    live                  INTEGER NOT NULL DEFAULT 0,
    event_id              INTEGER,
    escalation_message_id INTEGER,
    room_code             TEXT,
    PRIMARY KEY (tournament_id, match_id)
);

CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    match_id      INTEGER NOT NULL,
    proposer_id   INTEGER NOT NULL,
    responder_id  INTEGER,
    proposed_at   TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    status        TEXT    NOT NULL DEFAULT 'pending',
    message_id    INTEGER
);

CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    match_id      INTEGER NOT NULL,
    fire_at       TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    sent          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_usage (
    month TEXT    PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_usage_day (
    day    TEXT    NOT NULL,
    reason TEXT    NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, reason)
);

-- @@INDEXES@@

CREATE INDEX IF NOT EXISTS idx_tournaments_guild
    ON tournaments (guild_id, archived);
CREATE INDEX IF NOT EXISTS idx_tournaments_due
    ON tournaments (archived, next_refresh_at);
CREATE INDEX IF NOT EXISTS idx_participants_discord
    ON participants (tournament_id, discord_user_id);
CREATE INDEX IF NOT EXISTS idx_matches_state
    ON matches (tournament_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_thread
    ON matches (thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_proposals_match
    ON proposals (tournament_id, match_id, status);
CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders (sent, fire_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_unique
    ON reminders (tournament_id, match_id, kind);
