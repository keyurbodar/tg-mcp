import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_tg_staging (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    chat_title TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    message_text TEXT NOT NULL,
    reply_to_msg_id TEXT,
    from_me BOOLEAN NOT NULL DEFAULT FALSE,
    msg_timestamp TIMESTAMP NOT NULL,
    processed BOOLEAN DEFAULT FALSE,
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS raw_tg_pending ON raw_tg_staging (processed, msg_timestamp);

CREATE TABLE IF NOT EXISTS tg_group_configs (
    chat_id TEXT PRIMARY KEY,
    chat_title TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('internal_ops', 'partner_bd', 'alpha_ecosystem', 'ignore')),
    is_active BOOLEAN DEFAULT TRUE,
    priority_level INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tg_action_items (
    id TEXT PRIMARY KEY,
    raw_tg_id TEXT REFERENCES raw_tg_staging(id),
    chat_id TEXT NOT NULL,
    chat_title TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    suggested_reply TEXT,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'sent', 'dismissed')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    raw_tg_id TEXT REFERENCES raw_tg_staging(id),
    chat_title TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS partner_briefs (
    id TEXT PRIMARY KEY,
    raw_tg_id TEXT REFERENCES raw_tg_staging(id),
    chat_title TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at INTEGER NOT NULL
);
"""

CATEGORIES = ("internal_ops", "partner_bd", "alpha_ecosystem", "ignore")
ALERT_CATEGORIES = ("internal_ops", "partner_bd")


def raw_id(chat_id: int | str, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def save_message(self, *, chat_id, chat_title, message_id, sender_id, sender_name,
                     message_text, reply_to_msg_id, from_me, msg_timestamp, default_category) -> bool:
        self.conn.execute(
            "INSERT OR IGNORE INTO tg_group_configs (chat_id, chat_title, category) VALUES (?, ?, ?)",
            (str(chat_id), chat_title, default_category),
        )
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO raw_tg_staging
               (id, chat_id, chat_title, message_id, sender_id, sender_name, message_text,
                reply_to_msg_id, from_me, msg_timestamp, processed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (raw_id(chat_id, message_id), str(chat_id), chat_title, message_id, str(sender_id),
             sender_name, message_text, str(reply_to_msg_id) if reply_to_msg_id else None,
             from_me, msg_timestamp.astimezone(timezone.utc).isoformat(), from_me),
        )
        if from_me and reply_to_msg_id:
            self.mark_processed([raw_id(chat_id, reply_to_msg_id)])
        if cur.rowcount == 0 or from_me:
            return False
        group = self.conn.execute(
            "SELECT is_active, category FROM tg_group_configs WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
        return bool(group and group["is_active"] and group["category"] in ALERT_CATEGORIES)

    def mark_processed(self, ids: list[str]) -> int:
        cur = self.conn.executemany(
            "UPDATE raw_tg_staging SET processed = TRUE WHERE id = ?", [(i,) for i in ids]
        )
        return cur.rowcount

    def pending(self, hours_back: float, categories: list[str], limit: int) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
        marks = ",".join("?" * len(categories))
        rows = self.conn.execute(
            f"""SELECT m.id AS raw_tg_id, m.chat_id, m.chat_title, g.category, g.priority_level,
                       m.message_id, m.sender_name, m.message_text, m.msg_timestamp,
                       m.reply_to_msg_id, p.sender_name AS reply_to_sender, p.message_text AS reply_to_text
                FROM raw_tg_staging m
                JOIN tg_group_configs g ON g.chat_id = m.chat_id
                LEFT JOIN raw_tg_staging p ON p.chat_id = m.chat_id AND p.message_id = CAST(m.reply_to_msg_id AS INTEGER)
                WHERE m.processed = FALSE AND m.from_me = FALSE AND m.msg_timestamp >= ?
                  AND g.is_active = TRUE AND g.category IN ({marks})
                ORDER BY g.priority_level, m.chat_title, m.msg_timestamp
                LIMIT ?""",
            (since, *categories, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def message(self, raw_tg_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM raw_tg_staging WHERE id = ?", (raw_tg_id,)).fetchone()
        return dict(row) if row else None

    def record_sent(self, original: dict | None, chat_id: str, chat_title: str, message: str):
        self.conn.execute(
            """INSERT INTO tg_action_items (id, raw_tg_id, chat_id, chat_title, sender_name, summary, suggested_reply, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'sent')""",
            (str(uuid.uuid4()), original["id"] if original else None, chat_id, chat_title,
             original["sender_name"] if original else "", original["message_text"][:280] if original else "",
             message),
        )
        if original:
            self.mark_processed([original["id"]])

    def route(self, raw_tg_id: str, table: str, payload: dict) -> str:
        assert table in ("signals", "partner_briefs")
        msg = self.message(raw_tg_id)
        if msg is None:
            raise ValueError(f"unknown raw_tg_id {raw_tg_id}")
        new_id = str(uuid.uuid4())
        self.conn.execute(
            f"INSERT INTO {table} (id, raw_tg_id, chat_title, payload) VALUES (?, ?, ?, ?)",
            (new_id, raw_tg_id, msg["chat_title"], json.dumps(payload)),
        )
        self.mark_processed([raw_tg_id])
        return new_id

    def save_oauth_token(self, token_hash: str, kind: str, client_id: str, scopes: str, resource: str | None, expires_at: int):
        self.conn.execute("DELETE FROM oauth_tokens WHERE expires_at < strftime('%s', 'now')")
        self.conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, client_id, scopes, resource, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash, kind, client_id, scopes, resource, expires_at),
        )

    def oauth_token(self, token_hash: str, kind: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ? AND expires_at >= strftime('%s', 'now')",
            (token_hash, kind),
        ).fetchone()
        return dict(row) if row else None

    def delete_oauth_token(self, token_hash: str):
        self.conn.execute("DELETE FROM oauth_tokens WHERE token_hash = ?", (token_hash,))

    def groups(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT g.*, COUNT(m.id) FILTER (WHERE m.processed = FALSE AND m.from_me = FALSE) AS unprocessed
               FROM tg_group_configs g LEFT JOIN raw_tg_staging m ON m.chat_id = g.chat_id
               GROUP BY g.chat_id ORDER BY g.priority_level, g.chat_title"""
        ).fetchall()
        return [dict(r) for r in rows]

    def set_group(self, chat_id: str, category: str | None, priority_level: int | None,
                  is_active: bool | None) -> dict:
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        cur = self.conn.execute(
            """UPDATE tg_group_configs SET category = COALESCE(?, category),
                   priority_level = COALESCE(?, priority_level), is_active = COALESCE(?, is_active)
               WHERE chat_id = ?""",
            (category, priority_level, is_active, chat_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"unknown chat_id {chat_id}; list_tg_groups shows known groups")
        return dict(self.conn.execute("SELECT * FROM tg_group_configs WHERE chat_id = ?", (chat_id,)).fetchone())
