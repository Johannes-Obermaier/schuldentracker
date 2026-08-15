import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def seed_authorized_chats(conn: sqlite3.Connection, chat_ids: list[int]) -> None:
    """Einmalige Erstbefüllung aus AUTHORIZED_CHAT_IDS -- nur wenn die Tabelle noch
    leer ist, damit per /deauthorize entfernte Chats nicht bei jedem Start wiederkehren."""
    if conn.execute("SELECT COUNT(*) FROM authorized_users").fetchone()[0] > 0:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT OR IGNORE INTO authorized_users (chat_id, authorized_at) VALUES (?, ?)",
        [(chat_id, now) for chat_id in chat_ids],
    )
    conn.commit()


def is_chat_authorized(conn: sqlite3.Connection, chat_id: int) -> bool:
    return conn.execute("SELECT 1 FROM authorized_users WHERE chat_id = ?", (chat_id,)).fetchone() is not None


def authorize_chat(conn: sqlite3.Connection, chat_id: int) -> bool:
    """Gibt True zurück, wenn der Chat neu hinzugefügt wurde (False = war schon autorisiert)."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO authorized_users (chat_id, authorized_at) VALUES (?, ?)",
        (chat_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cursor.rowcount > 0


def deauthorize_chat(conn: sqlite3.Connection, chat_id: int) -> bool:
    cursor = conn.execute("DELETE FROM authorized_users WHERE chat_id = ?", (chat_id,))
    conn.commit()
    return cursor.rowcount > 0


def touch_user_profile(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    first_name: str | None,
    last_name: str | None,
    username: str | None,
    language_code: str | None,
    is_premium: bool | None,
) -> None:
    """Aktualisiert die bei Telegram bekannten Profildaten eines bereits autorisierten Chats.
    Wirkungslos, falls der Chat (noch) nicht autorisiert ist."""
    conn.execute(
        """
        UPDATE authorized_users
        SET first_name = ?, last_name = ?, username = ?, language_code = ?,
            is_premium = ?, last_seen_at = ?
        WHERE chat_id = ?
        """,
        (
            first_name, last_name, username, language_code,
            int(is_premium) if is_premium is not None else None,
            datetime.now(timezone.utc).isoformat(),
            chat_id,
        ),
    )
    conn.commit()


def list_authorized_chats(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT chat_id FROM authorized_users ORDER BY chat_id").fetchall()
    return [row["chat_id"] for row in rows]


def get_user_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM authorized_users ORDER BY chat_id").fetchall()
