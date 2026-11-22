import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    chat_type TEXT NOT NULL CHECK (chat_type IN ('dm', 'bot', 'group', 'channel')),
    read_inbox_max_id INTEGER NOT NULL DEFAULT 0,
    unread_count INTEGER NOT NULL DEFAULT 0,
    unread_mentions_count INTEGER NOT NULL DEFAULT 0,
    unread_mark BOOLEAN NOT NULL DEFAULT FALSE,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    is_muted BOOLEAN NOT NULL DEFAULT FALSE,
    watch_mode TEXT NOT NULL DEFAULT 'all' CHECK (watch_mode IN ('all', 'mentions_only', 'off')),
    last_message_id INTEGER NOT NULL DEFAULT 0,
    last_message_at TIMESTAMP,
    last_message_preview TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    message_text TEXT NOT NULL,
    reply_to_msg_id INTEGER,
    from_me BOOLEAN NOT NULL DEFAULT FALSE,
    is_mention BOOLEAN NOT NULL DEFAULT FALSE,
    is_reply_to_me BOOLEAN NOT NULL DEFAULT FALSE,
    msg_timestamp TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat_msg ON messages (chat_id, message_id);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (msg_timestamp);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at INTEGER NOT NULL
);
"""

CHAT_TYPES = ("dm", "bot", "group", "channel")
WATCH_MODES = ("all", "mentions_only", "off")


def msg_pk(chat_id: int | str, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def default_watch_mode(chat_type: str) -> str:
    return "off" if chat_type in ("channel", "bot") else "all"


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate_if_needed()
        self.conn.executescript(SCHEMA)

    def _migrate_if_needed(self) -> None:
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chats'"
        ).fetchone()
        if row and "'bot'" not in (row["sql"] or ""):
            self.conn.executescript("""
                ALTER TABLE chats RENAME TO chats_old;
                CREATE TABLE chats (
                    chat_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    username TEXT,
                    chat_type TEXT NOT NULL CHECK (chat_type IN ('dm', 'bot', 'group', 'channel')),
                    read_inbox_max_id INTEGER NOT NULL DEFAULT 0,
                    unread_count INTEGER NOT NULL DEFAULT 0,
                    unread_mentions_count INTEGER NOT NULL DEFAULT 0,
                    unread_mark BOOLEAN NOT NULL DEFAULT FALSE,
                    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
                    is_muted BOOLEAN NOT NULL DEFAULT FALSE,
                    watch_mode TEXT NOT NULL DEFAULT 'all' CHECK (watch_mode IN ('all', 'mentions_only', 'off')),
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    last_message_at TIMESTAMP,
                    last_message_preview TEXT
                );
                INSERT INTO chats (
                    chat_id, title, username, chat_type, read_inbox_max_id,
                    unread_count, unread_mentions_count, watch_mode,
                    last_message_id, last_message_at, last_message_preview
                )
                SELECT chat_id, title, username, chat_type, read_inbox_max_id,
                       unread_count, unread_mentions_count, watch_mode,
                       last_message_id, last_message_at, last_message_preview
                FROM chats_old;
                DROP TABLE chats_old;
            """)

    def upsert_chat(
        self,
        *,
        chat_id: int | str,
        title: str,
        username: str | None = None,
        chat_type: str = "group",
        read_inbox_max_id: int | None = None,
        unread_count: int | None = None,
        unread_mentions_count: int | None = None,
        unread_mark: bool | None = None,
        is_archived: bool | None = None,
        is_muted: bool | None = None,
    ) -> None:
        cid = str(chat_id)
        watch = default_watch_mode(chat_type)
        self.conn.execute(
            """INSERT INTO chats (
                   chat_id, title, username, chat_type, read_inbox_max_id,
                   unread_count, unread_mentions_count, unread_mark, is_archived, is_muted, watch_mode
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   title = excluded.title,
                   username = COALESCE(excluded.username, chats.username),
                   chat_type = excluded.chat_type,
                   read_inbox_max_id = CASE
                       WHEN ? IS NOT NULL AND ? > chats.read_inbox_max_id THEN ?
                       ELSE chats.read_inbox_max_id
                   END,
                   unread_count = COALESCE(?, chats.unread_count),
                   unread_mentions_count = COALESCE(?, chats.unread_mentions_count),
                   unread_mark = COALESCE(?, chats.unread_mark),
                   is_archived = COALESCE(?, chats.is_archived),
                   is_muted = COALESCE(?, chats.is_muted),
                   watch_mode = CASE
                       WHEN excluded.chat_type = 'bot' AND chats.chat_type != 'bot' THEN 'off'
                       ELSE chats.watch_mode
                   END""",
            (
                cid, title, username, chat_type,
                read_inbox_max_id or 0, unread_count or 0, unread_mentions_count or 0,
                bool(unread_mark) if unread_mark is not None else False,
                bool(is_archived) if is_archived is not None else False,
                bool(is_muted) if is_muted is not None else False,
                watch,
                read_inbox_max_id, read_inbox_max_id, read_inbox_max_id,
                unread_count, unread_mentions_count,
                bool(unread_mark) if unread_mark is not None else None,
                bool(is_archived) if is_archived is not None else None,
                bool(is_muted) if is_muted is not None else None,
            ),
        )

    def save_message(
        self,
        *,
        chat_id: int | str,
        title: str,
        username: str | None = None,
        chat_type: str = "group",
        message_id: int,
        sender_id: int | str,
        sender_name: str,
        message_text: str,
        reply_to_msg_id: int | None,
        from_me: bool,
        is_mention: bool = False,
        msg_timestamp: datetime,
    ) -> bool:
        cid = str(chat_id)
        self.upsert_chat(chat_id=cid, title=title, username=username, chat_type=chat_type)

        is_reply_to_me = False
        if not from_me and reply_to_msg_id:
            parent = self.conn.execute(
                "SELECT from_me FROM messages WHERE chat_id = ? AND message_id = ?",
                (cid, int(reply_to_msg_id)),
            ).fetchone()
            if parent and parent["from_me"]:
                is_reply_to_me = True

        ts = msg_timestamp.astimezone(timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO messages
               (id, chat_id, message_id, sender_id, sender_name, message_text,
                reply_to_msg_id, from_me, is_mention, is_reply_to_me, msg_timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg_pk(cid, message_id), cid, int(message_id), str(sender_id), sender_name,
                message_text, int(reply_to_msg_id) if reply_to_msg_id else None,
                bool(from_me), bool(is_mention), bool(is_reply_to_me), ts,
            ),
        )

        self.conn.execute(
            """UPDATE chats SET
                   last_message_id = CASE WHEN ? > last_message_id THEN ? ELSE last_message_id END,
                   last_message_at = CASE WHEN ? >= last_message_id THEN ? ELSE last_message_at END,
                   last_message_preview = CASE WHEN ? >= last_message_id THEN ? ELSE last_message_preview END
               WHERE chat_id = ?""",
            (int(message_id), int(message_id), int(message_id), ts, int(message_id), message_text[:200], cid),
        )

        if from_me:
            self.mark_chat_read(cid, int(message_id))
            return False

        if cur.rowcount == 0:
            return False

        chat = self.conn.execute(
            "SELECT read_inbox_max_id, watch_mode, is_archived FROM chats WHERE chat_id = ?", (cid,)
        ).fetchone()
        if not chat:
            return False

        is_unread = int(message_id) > int(chat["read_inbox_max_id"])
        if is_unread:
            self.conn.execute(
                """UPDATE chats SET
                       unread_count = unread_count + 1,
                       unread_mentions_count = unread_mentions_count + ?
                   WHERE chat_id = ?""",
                (1 if (is_mention or is_reply_to_me) else 0, cid),
            )

        mode = chat["watch_mode"]
        if not is_unread or mode == "off" or chat["is_archived"]:
            return False
        if mode == "mentions_only":
            return bool(is_mention or is_reply_to_me)
        return True

    def mark_chat_read(self, chat_id: int | str, max_id: int | None = None) -> dict:
        cid = str(chat_id)
        row = self.conn.execute(
            "SELECT read_inbox_max_id, last_message_id FROM chats WHERE chat_id = ?", (cid,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown chat_id {cid}; list_chats shows known chats")
        target_id = max(int(row["read_inbox_max_id"]), int(max_id if max_id is not None else row["last_message_id"]))
        counts = self.conn.execute(
            """SELECT
                   COUNT(*) AS unread_count,
                   COUNT(*) FILTER (WHERE is_mention = TRUE OR is_reply_to_me = TRUE) AS unread_mentions
               FROM messages
               WHERE chat_id = ? AND from_me = FALSE AND message_id > ?""",
            (cid, target_id),
        ).fetchone()
        self.conn.execute(
            """UPDATE chats SET
                   read_inbox_max_id = ?,
                   unread_count = ?,
                   unread_mentions_count = ?,
                   unread_mark = FALSE
               WHERE chat_id = ?""",
            (target_id, int(counts["unread_count"]), int(counts["unread_mentions"]), cid),
        )
        return dict(self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone())

    def list_chats(
        self,
        chat_type: str = "all",
        unread_only: bool = False,
        include_bots: bool = False,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        clauses = []
        params: list = []
        if chat_type != "all":
            clauses.append("chat_type = ?")
            params.append(chat_type)
        elif not include_bots:
            clauses.append("chat_type != 'bot'")
        if unread_only:
            clauses.append("(unread_count > 0 OR unread_mark = TRUE)")
        if not include_archived:
            clauses.append("is_archived = FALSE")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT chat_id, title, username, chat_type, unread_count, unread_mentions_count,
                       unread_mark, is_archived, is_muted, read_inbox_max_id, watch_mode,
                       last_message_id, last_message_at, last_message_preview
                FROM chats {where}
                ORDER BY (unread_count > 0 OR unread_mark = TRUE) DESC, (chat_type = 'dm') DESC, last_message_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unread(
        self,
        *,
        chat_type: str = "all",
        mentions_only: bool = False,
        include_bots: bool = False,
        include_archived: bool = False,
        hours_back: float | None = None,
        per_chat_limit: int = 10,
        max_chats: int = 50,
    ) -> list[dict]:
        chat_clauses = ["(unread_count > 0 OR unread_mark = TRUE)"]
        chat_params: list = []
        if chat_type != "all":
            chat_clauses.append("chat_type = ?")
            chat_params.append(chat_type)
        elif not include_bots:
            chat_clauses.append("chat_type != 'bot'")
        if not include_archived:
            chat_clauses.append("is_archived = FALSE")
        chat_params.append(max_chats)

        unread_chats = self.conn.execute(
            f"""SELECT chat_id, title, username, chat_type, unread_count, unread_mentions_count,
                       unread_mark, is_muted, read_inbox_max_id, watch_mode, last_message_at, last_message_preview
                FROM chats
                WHERE {' AND '.join(chat_clauses)}
                ORDER BY (chat_type = 'dm') DESC, last_message_at DESC
                LIMIT ?""",
            chat_params,
        ).fetchall()

        since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat() if hours_back is not None else None
        result: list[dict] = []
        for c in unread_chats:
            msg_clauses = ["m.chat_id = ?", "m.from_me = FALSE"]
            msg_params: list = [c["chat_id"]]
            if not c["unread_mark"] or c["unread_count"] > 0:
                msg_clauses.append("m.message_id > ?")
                msg_params.append(int(c["read_inbox_max_id"]))
            if since is not None:
                msg_clauses.append("m.msg_timestamp >= ?")
                msg_params.append(since)
            if mentions_only and c["chat_type"] != "dm":
                msg_clauses.append("(m.is_mention = TRUE OR m.is_reply_to_me = TRUE)")
            msg_params.append(per_chat_limit)

            rows = self.conn.execute(
                f"""SELECT m.id AS msg_uid, m.message_id, m.sender_name, m.message_text,
                           m.msg_timestamp, m.is_mention, m.is_reply_to_me,
                           m.reply_to_msg_id, p.sender_name AS reply_to_sender, p.message_text AS reply_to_text
                    FROM messages m
                    LEFT JOIN messages p ON p.chat_id = m.chat_id AND p.message_id = m.reply_to_msg_id
                    WHERE {' AND '.join(msg_clauses)}
                    ORDER BY m.message_id DESC
                    LIMIT ?""",
                msg_params,
            ).fetchall()
            if mentions_only and c["chat_type"] != "dm" and not rows:
                continue
            if since is not None and not rows:
                continue
            msgs = list(reversed([dict(r) for r in rows]))
            result.append({
                "chat_id": c["chat_id"],
                "chat_title": c["title"],
                "chat_username": c["username"],
                "chat_type": c["chat_type"],
                "unread_count": int(c["unread_count"]),
                "unread_mentions_count": int(c["unread_mentions_count"]),
                "unread_mark": bool(c["unread_mark"]),
                "is_muted": bool(c["is_muted"]),
                "watch_mode": c["watch_mode"],
                "last_message_at": c["last_message_at"],
                "messages_truncated": int(c["unread_count"]) > len(msgs),
                "messages": msgs,
            })
        return result

    def get_messages(self, chat_id: str, limit: int = 50, before_id: int | None = None) -> list[dict]:
        params: list = [str(chat_id)]
        before_sql = ""
        if before_id is not None:
            before_sql = "AND m.message_id < ?"
            params.append(int(before_id))
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT m.id AS msg_uid, m.chat_id, m.message_id, m.sender_name, m.message_text,
                       m.from_me, m.is_mention, m.is_reply_to_me, m.msg_timestamp,
                       m.reply_to_msg_id, p.sender_name AS reply_to_sender, p.message_text AS reply_to_text
                FROM messages m
                LEFT JOIN messages p ON p.chat_id = m.chat_id AND p.message_id = m.reply_to_msg_id
                WHERE m.chat_id = ? {before_sql}
                ORDER BY m.message_id DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def search_messages(self, query: str, chat_id: str | None = None, limit: int = 50) -> list[dict]:
        clauses = ["m.message_text LIKE ?"]
        params: list = [f"%{query}%"]
        if chat_id is not None:
            clauses.append("m.chat_id = ?")
            params.append(str(chat_id))
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT m.id AS msg_uid, m.chat_id, c.title AS chat_title, c.chat_type,
                       m.message_id, m.sender_name, m.message_text, m.from_me, m.msg_timestamp
                FROM messages m
                JOIN chats c ON c.chat_id = m.chat_id
                WHERE {' AND '.join(clauses)}
                ORDER BY m.msg_timestamp DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def set_chat_watch(self, chat_id: str, watch_mode: str) -> dict:
        if watch_mode not in WATCH_MODES:
            raise ValueError(f"watch_mode must be one of {WATCH_MODES}")
        cur = self.conn.execute(
            "UPDATE chats SET watch_mode = ? WHERE chat_id = ?", (watch_mode, str(chat_id))
        )
        if cur.rowcount == 0:
            raise ValueError(f"unknown chat_id {chat_id}; list_chats shows known chats")
        return dict(self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (str(chat_id),)).fetchone())

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
