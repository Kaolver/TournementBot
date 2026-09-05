-- Supabase setup script for TournamentBot.
-- Safe to re-run: uses IF NOT EXISTS and OR REPLACE.

CREATE SCHEMA IF NOT EXISTS tournamentbot;

-- bot_sql executes statements via service_role in tournamentbot schema.
CREATE OR REPLACE FUNCTION tournamentbot.bot_sql(q text, args jsonb DEFAULT '[]'::jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = tournamentbot, public
AS $$
DECLARE
    statement text := q;
    value     text;
    n         int := jsonb_array_length(coalesce(args, '[]'::jsonb));
    i         int;
    rows      jsonb;
    affected  bigint;
BEGIN
    -- Descending, so $1 does not match the start of $10.
    FOR i IN REVERSE n..1 LOOP
        IF jsonb_typeof(args -> (i - 1)) = 'null' THEN
            value := 'NULL';
        ELSE
            value := quote_nullable(args ->> (i - 1));
        END IF;
        statement := replace(statement, '$' || i::text, value);
    END LOOP;

    -- Anything that hands rows back is wrapped so the result is one jsonb value.
    IF statement ~* '^\s*(select|with)\s' OR statement ~* '\sreturning\s' THEN
        EXECUTE 'SELECT coalesce(jsonb_agg(t), ''[]''::jsonb) FROM (' || statement || ') t'
            INTO rows;
        RETURN jsonb_build_object(
            'rows', rows,
            'rowcount', jsonb_array_length(rows)
        );
    END IF;

    EXECUTE statement;
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN jsonb_build_object('rows', '[]'::jsonb, 'rowcount', affected);
END;
$$;

REVOKE ALL ON FUNCTION tournamentbot.bot_sql(text, jsonb) FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION tournamentbot.bot_sql(text, jsonb) TO service_role;

GRANT USAGE ON SCHEMA tournamentbot TO service_role;

-- Tables

CREATE TABLE IF NOT EXISTS tournamentbot.guilds (
    guild_id              BIGINT PRIMARY KEY,
    tournament_channel_id BIGINT,
    to_role_id            BIGINT,
    timezone              TEXT    NOT NULL DEFAULT 'UTC',
    deadline_hours        INTEGER NOT NULL DEFAULT 24,
    auto_sync             INTEGER NOT NULL DEFAULT 1,
    syncs_per_day         INTEGER NOT NULL DEFAULT 12,
    event_channel_id      BIGINT,
    event_location        TEXT,
    event_duration        INTEGER NOT NULL DEFAULT 60,
    first_match_day       TEXT,
    days_per_round        INTEGER NOT NULL DEFAULT 0,
    round_days            TEXT
);

CREATE TABLE IF NOT EXISTS tournamentbot.tournaments (
    challonge_id         BIGINT PRIMARY KEY,
    guild_id             BIGINT NOT NULL,
    name                 TEXT   NOT NULL,
    url                  TEXT,
    full_url             TEXT,
    tournament_type      TEXT,
    state                TEXT   NOT NULL DEFAULT 'pending',
    channel_id           BIGINT,
    signup_message_id    BIGINT,
    bracket_message_id   BIGINT,
    archived             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT   NOT NULL DEFAULT ((now() at time zone 'utc')::text),
    next_refresh_at      TEXT,
    last_refresh_at      TEXT,
    refresh_window_start TEXT,
    refresh_window_count INTEGER NOT NULL DEFAULT 0,
    sync_day             TEXT,
    sync_day_count       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tournamentbot.participants (
    tournament_id   BIGINT NOT NULL,
    participant_id  BIGINT NOT NULL,
    discord_user_id BIGINT,
    name            TEXT   NOT NULL,
    seed            INTEGER,
    final_rank      INTEGER,
    PRIMARY KEY (tournament_id, participant_id)
);

CREATE TABLE IF NOT EXISTS tournamentbot.signups (
    tournament_id   BIGINT NOT NULL,
    discord_user_id BIGINT NOT NULL,
    name            TEXT   NOT NULL,
    seed            INTEGER,
    signed_up_at    TEXT   NOT NULL DEFAULT ((now() at time zone 'utc')::text),
    PRIMARY KEY (tournament_id, discord_user_id)
);

CREATE TABLE IF NOT EXISTS tournamentbot.matches (
    tournament_id     BIGINT NOT NULL,
    match_id          BIGINT NOT NULL,
    identifier        TEXT,
    round             INTEGER NOT NULL DEFAULT 0,
    play_order        INTEGER,
    player1_id        BIGINT,
    player2_id        BIGINT,
    state             TEXT   NOT NULL DEFAULT 'pending',
    winner_id         BIGINT,
    scores            TEXT,
    thread_id         BIGINT,
    scheduled_at      TEXT,
    agreed_at         TEXT,
    deadline_at       TEXT,
    play_by           TEXT,
    scheduling_status TEXT   NOT NULL DEFAULT 'pending',
    live              INTEGER NOT NULL DEFAULT 0,
    event_id          BIGINT,
    escalation_message_id BIGINT,
    room_code         TEXT,
    PRIMARY KEY (tournament_id, match_id)
);

CREATE TABLE IF NOT EXISTS tournamentbot.proposals (
    id            BIGSERIAL PRIMARY KEY,
    tournament_id BIGINT NOT NULL,
    match_id      BIGINT NOT NULL,
    proposer_id   BIGINT NOT NULL,
    responder_id  BIGINT,
    proposed_at   TEXT   NOT NULL,
    created_at    TEXT   NOT NULL DEFAULT ((now() at time zone 'utc')::text),
    status        TEXT   NOT NULL DEFAULT 'pending',
    message_id    BIGINT
);

CREATE TABLE IF NOT EXISTS tournamentbot.reminders (
    id            BIGSERIAL PRIMARY KEY,
    tournament_id BIGINT NOT NULL,
    match_id      BIGINT NOT NULL,
    fire_at       TEXT   NOT NULL,
    kind          TEXT   NOT NULL,
    sent          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tournamentbot.api_usage (
    month TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tournamentbot.api_usage_day (
    day    TEXT NOT NULL,
    reason TEXT NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, reason)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tournaments_guild
    ON tournamentbot.tournaments (guild_id, archived);
CREATE INDEX IF NOT EXISTS idx_tournaments_due
    ON tournamentbot.tournaments (archived, next_refresh_at);
CREATE INDEX IF NOT EXISTS idx_participants_discord
    ON tournamentbot.participants (tournament_id, discord_user_id);
CREATE INDEX IF NOT EXISTS idx_matches_state
    ON tournamentbot.matches (tournament_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_thread
    ON tournamentbot.matches (thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_proposals_match
    ON tournamentbot.proposals (tournament_id, match_id, status);
CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON tournamentbot.reminders (sent, fire_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_unique
    ON tournamentbot.reminders (tournament_id, match_id, kind);

-- Row Level Security
ALTER TABLE tournamentbot.guilds        ENABLE ROW LEVEL SECURITY;
ALTER TABLE tournamentbot.tournaments   ENABLE ROW LEVEL SECURITY;
ALTER TABLE tournamentbot.participants  ENABLE ROW LEVEL SECURITY;
ALTER TABLE tournamentbot.signups       ENABLE ROW LEVEL SECURITY;
ALTER TABLE tournamentbot.matches       ENABLE ROW LEVEL SECURITY;
ALTER TABLE tournamentbot.proposals     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tournamentbot.reminders     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tournamentbot.api_usage     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tournamentbot.api_usage_day ENABLE ROW LEVEL SECURITY;

-- Relay match registry
CREATE TABLE IF NOT EXISTS public.dm_matches (
    code        TEXT        PRIMARY KEY,
    match_id    TEXT        NOT NULL,
    tournament  TEXT        NOT NULL DEFAULT '',
    players     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    best_of     INT         NOT NULL DEFAULT 1,
    state       TEXT        NOT NULL DEFAULT 'open',
    winner_seat INT,
    scores      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reported_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS dm_matches_state
    ON public.dm_matches (state, created_at DESC);
ALTER TABLE public.dm_matches ENABLE ROW LEVEL SECURITY;
