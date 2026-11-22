import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import uvicorn
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.types import Channel

from .db import DB
from .oauth import Provider
from .server import PENDING_PINGS_URI, PingNotifier, build_app

log = logging.getLogger("hub_tg")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"missing required env var {name}")
    return value


def default_category(chat, fallback: str) -> str:
    if isinstance(chat, Channel) and chat.broadcast:
        return "alpha_ecosystem"
    return fallback


class Telegram:
    def __init__(
        self,
        client: TelegramClient,
        db: DB,
        fallback_category: str,
        on_new_ping: Callable[[], Awaitable[None]] | None = None,
    ):
        self.client = client
        self.db = db
        self.fallback_category = fallback_category
        self.on_new_ping = on_new_ping

    async def ingest(self, message, chat, *, notify: bool = False):
        if message.action is not None:
            return
        sender = await message.get_sender()
        name = utils.get_display_name(sender) if sender else ""
        is_new_ping = self.db.save_message(
            chat_id=message.chat_id,
            chat_title=getattr(chat, "title", None) or "Group",
            message_id=message.id,
            sender_id=message.sender_id or 0,
            sender_name=name or "Unknown",
            message_text=message.raw_text or ("[media]" if message.media else ""),
            reply_to_msg_id=message.reply_to_msg_id,
            from_me=bool(message.out),
            msg_timestamp=message.date,
            default_category=default_category(chat, self.fallback_category),
        )
        if notify and is_new_ping and self.on_new_ping is not None:
            await self.on_new_ping()

    async def backfill(self, hours: float, per_chat_limit: int):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        count = 0
        async for dialog in self.client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel) or dialog.date is None or dialog.date < cutoff:
                continue
            async for message in self.client.iter_messages(dialog.entity, limit=per_chat_limit):
                if message.date < cutoff:
                    break
                await self.ingest(message, dialog.entity, notify=False)
                count += 1
        log.info("backfill stored up to %d messages from the last %sh", count, hours)

    async def send_reply(self, chat_id: str, reply_to_msg_id: int, message: str) -> int:
        try:
            entity = await self.client.get_input_entity(int(chat_id))
        except ValueError:
            await self.client.get_dialogs()
            entity = await self.client.get_input_entity(int(chat_id))
        sent = await self.client.send_message(entity, message, reply_to=reply_to_msg_id, parse_mode=None)
        return sent.id


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db = DB(os.environ.get("DATABASE_PATH", "/data/hub_tg.db"))
    notifier = PingNotifier()
    client = TelegramClient(StringSession(env("TELEGRAM_SESSION_STRING")), int(env("TELEGRAM_API_ID")), env("TELEGRAM_API_HASH"))
    tg = Telegram(
        client,
        db,
        os.environ.get("TG_DEFAULT_CATEGORY", "internal_ops"),
        on_new_ping=lambda: notifier.notify_resource_updated(PENDING_PINGS_URI),
    )

    @client.on(events.NewMessage)
    async def on_message(event):
        if not (event.is_group or event.is_channel):
            return
        try:
            await tg.ingest(event.message, await event.get_chat(), notify=True)
        except Exception:
            log.exception("failed to store message %s in %s", event.id, event.chat_id)

    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("TELEGRAM_SESSION_STRING is not authorized; run: docker compose run --rm tg python -m hub_tg.login")
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
