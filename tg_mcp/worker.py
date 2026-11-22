import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import uvicorn
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import Channel, ReactionEmoji, User

from .db import DB
from .oauth import Provider
from .server import UNREAD_INBOX_URI, InboxNotifier, build_app

log = logging.getLogger("tg_mcp")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"missing required env var {name}")
    return value


def resolve_db_path() -> str:
    if os.environ.get("DATABASE_PATH"):
        return os.environ["DATABASE_PATH"]
    if os.path.exists("/data/hub_tg.db"):
        return "/data/hub_tg.db"
    return "/data/tg_mcp.db"


def classify_chat(chat) -> tuple[str, str, str | None]:
    if isinstance(chat, User):
        title = utils.get_display_name(chat) or chat.username or str(chat.id)
        chat_type = "bot" if getattr(chat, "bot", False) else "dm"
        return chat_type, title, getattr(chat, "username", None)
    if isinstance(chat, Channel) and chat.broadcast:
        return "channel", getattr(chat, "title", None) or "Channel", getattr(chat, "username", None)
    return "group", getattr(chat, "title", None) or "Group", getattr(chat, "username", None)


def dialog_is_muted(raw_dialog) -> bool:
    settings = getattr(raw_dialog, "notify_settings", None)
    mute_until = getattr(settings, "mute_until", None)
    if not mute_until:
        return False
    if isinstance(mute_until, datetime):
        return mute_until > datetime.now(timezone.utc)
    if isinstance(mute_until, int):
        return mute_until > int(datetime.now(timezone.utc).timestamp())
    return False


class Telegram:
    def __init__(
        self,
        client: TelegramClient,
        db: DB,
        on_new_unread: Callable[[], Awaitable[None]] | None = None,
    ):
        self.client = client
        self.db = db
        self.on_new_unread = on_new_unread

    async def _resolve_entity(self, chat_id: str):
        try:
            return await self.client.get_input_entity(int(chat_id))
        except ValueError:
            await self.client.get_dialogs()
            return await self.client.get_input_entity(int(chat_id))

    async def ingest(self, message, chat, *, notify: bool = False):
        if message.action is not None:
            return
        chat_type, title, username = classify_chat(chat)
        sender = await message.get_sender()
        name = utils.get_display_name(sender) if sender else ""
        should_notify = self.db.save_message(
            chat_id=message.chat_id,
            title=title,
            username=username,
            chat_type=chat_type,
            message_id=message.id,
            sender_id=message.sender_id or 0,
            sender_name=name or "Unknown",
            message_text=message.raw_text or ("[media]" if message.media else ""),
            reply_to_msg_id=message.reply_to_msg_id,
            from_me=bool(message.out),
            is_mention=bool(getattr(message, "mentioned", False)),
            msg_timestamp=message.date,
        )
        if notify and should_notify and self.on_new_unread is not None:
            await self.on_new_unread()

    async def backfill(self, hours: float, per_chat_limit: int):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        chat_count = 0
        msg_count = 0
        async for dialog in self.client.iter_dialogs():
            chat_type, title, username = classify_chat(dialog.entity)
            raw_dialog = getattr(dialog, "dialog", None)
            read_max = int(getattr(raw_dialog, "read_inbox_max_id", 0) or 0)
            unread_count = int(getattr(dialog, "unread_count", 0) or 0)
            unread_mentions = int(getattr(dialog, "unread_mentions_count", 0) or 0)
            unread_mark = bool(getattr(raw_dialog, "unread_mark", False))
            is_archived = bool(getattr(dialog, "archived", False))
            is_muted = dialog_is_muted(raw_dialog)

            self.db.upsert_chat(
                chat_id=dialog.id,
                title=title,
                username=username,
                chat_type=chat_type,
                read_inbox_max_id=read_max,
                unread_count=unread_count,
                unread_mentions_count=unread_mentions,
                unread_mark=unread_mark,
                is_archived=is_archived,
                is_muted=is_muted,
            )
            chat_count += 1

            has_unread = unread_count > 0 or unread_mark
            is_recent = dialog.date is not None and dialog.date >= cutoff
            if not is_recent and not has_unread:
                continue

            fetch_limit = per_chat_limit if is_recent else min(max(unread_count, 20), 50)
            async for message in self.client.iter_messages(dialog.entity, limit=fetch_limit):
                if is_recent and message.date < cutoff and message.id <= read_max:
                    break
                await self.ingest(message, dialog.entity, notify=False)
                msg_count += 1

            # Re-assert authoritative Telegram unread/archived/muted state after backfill inserts
            self.db.upsert_chat(
                chat_id=dialog.id,
                title=title,
                username=username,
                chat_type=chat_type,
                read_inbox_max_id=read_max,
                unread_count=unread_count,
                unread_mentions_count=unread_mentions,
                unread_mark=unread_mark,
                is_archived=is_archived,
                is_muted=is_muted,
            )
        log.info("backfill synced %d chats and stored up to %d messages from the last %sh", chat_count, msg_count, hours)

    async def fetch_messages(self, chat_id: str, limit: int = 50, before_id: int | None = None) -> list[dict[str, Any]]:
        entity = await self._resolve_entity(chat_id)
        chat = await self.client.get_entity(entity)
        kwargs: dict[str, Any] = {"limit": limit}
        if before_id is not None:
            kwargs["max_id"] = int(before_id)
        async for message in self.client.iter_messages(entity, **kwargs):
            await self.ingest(message, chat, notify=False)
        return self.db.get_messages(chat_id=chat_id, limit=limit, before_id=before_id)

    async def send_message(self, chat_id: str, message: str, reply_to_msg_id: int | None = None) -> int:
        entity = await self._resolve_entity(chat_id)
        sent = await self.client.send_message(entity, message, reply_to=reply_to_msg_id, parse_mode=None)
        return sent.id

    async def send_reaction(self, chat_id: str, message_id: int, emoji: str) -> None:
        entity = await self._resolve_entity(chat_id)
        reaction = [ReactionEmoji(emoticon=emoji)] if emoji else []
        await self.client(SendReactionRequest(peer=entity, msg_id=message_id, reaction=reaction))

    async def mark_read(self, chat_id: str, max_id: int | None = None) -> int | None:
        entity = await self._resolve_entity(chat_id)
        await self.client.send_read_acknowledge(entity, max_id=max_id)
        return max_id


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db = DB(resolve_db_path())
    notifier = InboxNotifier()
    client = TelegramClient(StringSession(env("TELEGRAM_SESSION_STRING")), int(env("TELEGRAM_API_ID")), env("TELEGRAM_API_HASH"))
    tg = Telegram(
        client,
        db,
        on_new_unread=lambda: notifier.notify_resource_updated(UNREAD_INBOX_URI),
    )

    @client.on(events.NewMessage)
    async def on_message(event):
        try:
            await tg.ingest(event.message, await event.get_chat(), notify=True)
        except Exception:
            log.exception("failed to store message %s in %s", event.id, event.chat_id)

    @client.on(events.MessageRead(inbox=True))
    async def on_read(event):
        try:
            db.mark_chat_read(event.chat_id, max_id=event.max_id)
        except Exception:
            log.debug("ignored read event for unknown chat %s", event.chat_id)

    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("TELEGRAM_SESSION_STRING is not authorized; run: docker compose run --rm tg python -m tg_mcp.login")
    me = await client.get_me()
    log.info("telegram connected as %s (%s)", utils.get_display_name(me), me.id)

    port = int(os.environ.get("PORT", "8080"))
    domain = os.environ.get("DOMAIN")
    provider = Provider(
        db,
        issuer=(os.environ.get("PUBLIC_URL") or (f"https://{domain}" if domain else f"http://localhost:{port}")).rstrip("/"),
        bearer_token=env("MCP_BEARER_TOKEN"),
        client_id=env("OAUTH_CLIENT_ID"),
        client_secret=env("OAUTH_CLIENT_SECRET"),
        redirect_uris=[u.strip() for u in os.environ.get("OAUTH_REDIRECT_URIS", "").split(",") if u.strip()],
        auth_method=os.environ.get("OAUTH_TOKEN_AUTH_METHOD", "client_secret_post"),
    )
    server = uvicorn.Server(uvicorn.Config(
        build_app(db, tg, provider, notifier=notifier), host="0.0.0.0", port=port,
        log_level="info", proxy_headers=True, forwarded_allow_ips="*",
    ))
    backfill = asyncio.create_task(tg.backfill(float(os.environ.get("BACKFILL_HOURS", "120")),
                                               int(os.environ.get("BACKFILL_PER_CHAT_LIMIT", "500"))))
    backfill.add_done_callback(lambda t: t.exception() and log.error("backfill failed: %r", t.exception()))
    done, _ = await asyncio.wait(
        [asyncio.create_task(server.serve()), asyncio.create_task(client.run_until_disconnected())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    sys.exit("telegram disconnected or http server stopped; exiting so docker restarts us")


if __name__ == "__main__":
    asyncio.run(main())
