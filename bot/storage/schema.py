"""SQLite schema for the Choi_bot log store.

Design notes that the DDL cannot express:
  * Discord snowflakes are TEXT. They exceed no SQLite limit, but keeping them
    textual avoids int/str drift across JSON, the API and future exports.
  * Timestamps are ISO-8601 TEXT. `event_time` is when the event happened,
    `recorded_at` is when the bot wrote it down. Legacy rows only ever know the
    latter, so `event_time` stays NULL rather than being guessed.
  * Occurrence identity is (source_id, byte_start, raw_hash) on message_origins,
    never content equality: the same sentence twice in the same second is two
    real messages.
"""

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS log_sources (
    id                  INTEGER PRIMARY KEY,
    source_kind         TEXT NOT NULL,
    relative_path       TEXT NOT NULL,
    original_path       TEXT,
    filename            TEXT NOT NULL,
    file_size           INTEGER NOT NULL,
    mtime               TEXT,
    sha256              TEXT NOT NULL,
    encoding            TEXT,
    newline_style       TEXT,
    has_bom             INTEGER NOT NULL DEFAULT 0,
    timezone_assumption TEXT,
    parser_version      TEXT,
    imported_at         TEXT NOT NULL,
    UNIQUE (sha256, relative_path)
);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY,
    event_uuid          TEXT NOT NULL UNIQUE,
    source_kind         TEXT NOT NULL,
    message_type        TEXT NOT NULL,
    raw_actor           TEXT,
    display_actor       TEXT,
    discord_user_id     TEXT,
    guild_id            TEXT,
    channel_id          TEXT,
    thread_id           TEXT,
    discord_message_id  TEXT,
    command_name        TEXT,
    content             TEXT NOT NULL,
    raw_timestamp       TEXT,
    event_time          TEXT,
    recorded_at         TEXT,
    local_date          TEXT,
    delivery_state      TEXT,
    control_kind        TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_origins (
    id           INTEGER PRIMARY KEY,
    message_id   INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_id    INTEGER NOT NULL REFERENCES log_sources(id) ON DELETE CASCADE,
    byte_start   INTEGER NOT NULL,
    byte_end     INTEGER NOT NULL,
    line_start   INTEGER,
    line_end     INTEGER,
    raw_hash     BLOB NOT NULL,   -- 32-byte sha256 digest, not hex text
    match_method TEXT NOT NULL,
    confidence   TEXT NOT NULL,
    UNIQUE (source_id, byte_start, raw_hash)
);

CREATE TABLE IF NOT EXISTS import_runs (
    id             INTEGER PRIMARY KEY,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    parser_version TEXT NOT NULL,
    mode           TEXT NOT NULL,
    source_count   INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    matched_count  INTEGER NOT NULL DEFAULT 0,
    skipped_count  INTEGER NOT NULL DEFAULT 0,
    issue_count    INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parse_issues (
    id               INTEGER PRIMARY KEY,
    import_run_id    INTEGER REFERENCES import_runs(id) ON DELETE CASCADE,
    source_id        INTEGER REFERENCES log_sources(id) ON DELETE CASCADE,
    byte_start       INTEGER,
    byte_end         INTEGER,
    line_start       INTEGER,
    line_end         INTEGER,
    issue_type       TEXT NOT NULL,
    raw_excerpt_hash BLOB,
    description      TEXT,
    resolution_state TEXT NOT NULL DEFAULT 'open',
    -- One row per outstanding issue, not per run: re-running the migration must
    -- not grow the worklist. Per-run totals live in import_runs.issue_count.
    UNIQUE (source_id, byte_start, issue_type)
);

-- Confirmed display mapping only. raw_actor on messages is never overwritten.
CREATE TABLE IF NOT EXISTS actor_aliases (
    id             INTEGER PRIMARY KEY,
    raw_actor      TEXT NOT NULL,
    display_actor  TEXT NOT NULL,
    mapping_source TEXT NOT NULL,
    mapping_version TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (raw_actor, mapping_version)
);

CREATE INDEX IF NOT EXISTS idx_messages_date_time ON messages(local_date, event_time, id);
CREATE INDEX IF NOT EXISTS idx_messages_actor_date ON messages(raw_actor, local_date);
CREATE INDEX IF NOT EXISTS idx_messages_type_date  ON messages(message_type, local_date);
CREATE INDEX IF NOT EXISTS idx_messages_recorded   ON messages(recorded_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_discord_id
    ON messages(discord_message_id) WHERE discord_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_origins_message ON message_origins(message_id);
-- Secondary sources are linked to their canonical twin by (timestamp, actor);
-- without this the merged exports would scan the whole table per record.
CREATE INDEX IF NOT EXISTS idx_messages_twin ON messages(raw_timestamp, raw_actor);
"""
