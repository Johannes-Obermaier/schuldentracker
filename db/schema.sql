CREATE TABLE IF NOT EXISTS authorized_users (
    chat_id INTEGER PRIMARY KEY,
    authorized_at TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    username TEXT,
    language_code TEXT,
    is_premium INTEGER,
    last_seen_at TEXT
);
