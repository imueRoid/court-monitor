CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY, court TEXT NOT NULL, case_number TEXT NOT NULL, party TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'pending',
    snapshot TEXT NOT NULL DEFAULT '{}', last_checked REAL, next_check REAL,
    last_error TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
    verdict_day TEXT NOT NULL DEFAULT '', verdict_count INTEGER NOT NULL DEFAULT 0,
    verdict_count_day TEXT NOT NULL DEFAULT '', manual INTEGER NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
    UNIQUE(court, case_number, party));
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), signature TEXT NOT NULL,
    hearing TEXT NOT NULL, start_ts REAL NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
    synced_hash TEXT NOT NULL DEFAULT '', synced_at REAL, next_sync REAL NOT NULL,
    last_error TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
    UNIQUE(case_id, signature));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, at REAL NOT NULL,
    level TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cases_due ON cases(next_check);
CREATE INDEX IF NOT EXISTS events_due ON events(next_sync);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires);
CREATE TABLE IF NOT EXISTS login_attempts (client_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL, started REAL NOT NULL);
CREATE TABLE IF NOT EXISTS google_oauth_states (
    state_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL, expires REAL NOT NULL,
    verifier TEXT NOT NULL, redirect_uri TEXT NOT NULL, generation INTEGER NOT NULL
);
