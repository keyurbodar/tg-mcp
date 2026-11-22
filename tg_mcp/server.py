import json
import logging
from typing import Annotated, Any, Literal, Protocol
from weakref import WeakSet

from mcp import types
from mcp.server.auth.settings import AuthSettings, RevocationOptions
from mcp.server.lowlevel.server import HandlerEntry
from mcp.server.mcpserver import MCPServer
from mcp.server.session import ServerSession
from mcp.server.subscriptions import ResourceUpdated, SubscriptionBus
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .db import DB
from .oauth import Provider

log = logging.getLogger("tg_mcp")

UNREAD_INBOX_URI = "tg://inbox/unread"


class TelegramBackend(Protocol):
    async def send_message(self, chat_id: str, message: str, reply_to_msg_id: int | None = None) -> int: ...
    async def send_reaction(self, chat_id: str, message_id: int, emoji: str) -> None: ...
    async def mark_read(self, chat_id: str, max_id: int | None = None) -> int | None: ...


class InboxNotifier:
    """Broadcasts MCP notifications/resources/updated to both modern (subscriptions/listen) and legacy (resources/subscribe) clients."""

    def __init__(self) -> None:
        self._bus: SubscriptionBus | None = None
        self._legacy_subscribers: dict[str, WeakSet[ServerSession]] = {}

    def attach(self, bus: SubscriptionBus) -> None:
        self._bus = bus

    def subscribe(self, uri: str, session: ServerSession) -> None:
        self._legacy_subscribers.setdefault(str(uri), WeakSet()).add(session)

    def unsubscribe(self, uri: str, session: ServerSession) -> None:
        subs = self._legacy_subscribers.get(str(uri))
        if subs is not None:
            subs.discard(session)

    async def notify_resource_updated(self, uri: str = UNREAD_INBOX_URI) -> None:
        target = str(uri)
        if self._bus is not None:
            try:
                await self._bus.publish(ResourceUpdated(uri=target))
            except Exception:
                log.exception("failed to publish subscription bus update for %s", target)

        subs = self._legacy_subscribers.get(target)
        if not subs:
            return
        stale: list[ServerSession] = []
        for session in list(subs):
            try:
                await session.send_resource_updated(target)
            except Exception:
                stale.append(session)
        for session in stale:
            subs.discard(session)


def _format_unread(db: DB, chat_type: str, mentions_only: bool, hours_back: float, limit: int) -> dict[str, Any]:
    chats: dict[str, dict] = {}
    rows = db.get_unread(chat_type=chat_type, mentions_only=mentions_only, hours_back=hours_back, limit=limit)
    for r in rows:
        c = chats.setdefault(r["chat_id"], {
            "chat_id": r["chat_id"],
            "chat_title": r["chat_title"],
            "chat_username": r["chat_username"],
            "chat_type": r["chat_type"],
            "watch_mode": r["watch_mode"],
            "messages": [],
        })
        c["messages"].append({k: r[k] for k in (
            "msg_uid", "message_id", "sender_name", "message_text", "msg_timestamp",
            "is_mention", "is_reply_to_me", "reply_to_msg_id", "reply_to_sender", "reply_to_text",
        )})
    return {"count": len(rows), "truncated": len(rows) == limit, "chats": list(chats.values())}


def build_mcp(db: DB, backend: TelegramBackend, provider: Provider, notifier: InboxNotifier | None = None) -> MCPServer:
    notifier = notifier or InboxNotifier()
    mcp = MCPServer(
        name="tg-mcp",
        instructions=(
            "Personal Telegram MCP covering DMs, groups, and channels with live Telegram read-state sync. "
            "Subscribe to tg://inbox/unread for instant push notifications. "
            "Never call send_message or react_message without the user's explicit approval first."
        ),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=provider.issuer,
            resource_server_url=f"{provider.issuer}/mcp",
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=False,
        ),
    )
    notifier.attach(mcp._subscriptions)

    async def on_subscribe(ctx, params: types.SubscribeRequestParams) -> types.EmptyResult:
        notifier.subscribe(str(params.uri), ctx.session)
        return types.EmptyResult()

    async def on_unsubscribe(ctx, params: types.UnsubscribeRequestParams) -> types.EmptyResult:
        notifier.unsubscribe(str(params.uri), ctx.session)
        return types.EmptyResult()

    mcp._lowlevel_server._request_handlers["resources/subscribe"] = HandlerEntry(
        types.SubscribeRequestParams, on_subscribe
    )
    mcp._lowlevel_server._request_handlers["resources/unsubscribe"] = HandlerEntry(
        types.UnsubscribeRequestParams, on_unsubscribe
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request):
        return PlainTextResponse("ok")

    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request):
        return await provider.login_page(request)

    @mcp.resource(
        UNREAD_INBOX_URI,
        name="unread_inbox",
        title="Unread Telegram Inbox",
        description="Unread messages across DMs, groups, and channels synced with Telegram read state. Subscribe for live notifications/resources/updated pushes.",
        mime_type="application/json",
    )
    def unread_inbox_resource() -> str:
        return json.dumps(_format_unread(db, chat_type="all", mentions_only=False, hours_back=72, limit=200))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_chats(
        chat_type: Literal["all", "dm", "group", "channel"] = "all",
        unread_only: bool = False,
        limit: Annotated[int, Field(gt=0, le=500)] = 100,
    ) -> dict[str, Any]:
        """List Telegram DMs, groups, and channels with live unread counts, mention counts, and last message preview."""
        chats = db.list_chats(chat_type=chat_type, unread_only=unread_only, limit=limit)
        return {"count": len(chats), "chats": chats}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_unread(
        chat_type: Literal["all", "dm", "group", "channel"] = "all",
        mentions_only: bool = False,
        hours_back: Annotated[float, Field(gt=0, le=720)] = 72,
        limit: Annotated[int, Field(gt=0, le=1000)] = 200,
    ) -> dict[str, Any]:
        """Get unread messages grouped by chat, synced with Telegram's actual read state. Set mentions_only=True to get all unread DMs plus direct mentions/replies in groups."""
        return _format_unread(db, chat_type, mentions_only, hours_back, limit)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_messages(
        chat_id: str,
        limit: Annotated[int, Field(gt=0, le=200)] = 50,
        before_id: int | None = None,
    ) -> dict[str, Any]:
        """Read recent messages in any DM, group, or channel in chronological order."""
        msgs = db.get_messages(chat_id=chat_id, limit=limit, before_id=before_id)
        return {"count": len(msgs), "chat_id": chat_id, "messages": msgs}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search_messages(
        query: Annotated[str, Field(min_length=1)],
        chat_id: str | None = None,
        limit: Annotated[int, Field(gt=0, le=200)] = 50,
    ) -> dict[str, Any]:
        """Search messages across all Telegram DMs, groups, and channels (or inside one specific chat_id)."""
        msgs = db.search_messages(query=query, chat_id=chat_id, limit=limit)
        return {"count": len(msgs), "query": query, "messages": msgs}

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
    async def send_message(
        chat_id: str,
        message: Annotated[str, Field(min_length=1)],
        reply_to_msg_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a message or threaded reply from the user's personal Telegram account to any DM or group. Requires explicit user approval first."""
        sent_id = await backend.send_message(chat_id, message, reply_to_msg_id=reply_to_msg_id)
        return {"sent_message_id": sent_id, "chat_id": chat_id, "reply_to_msg_id": reply_to_msg_id}

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
    async def react_message(
        chat_id: str,
        message_id: int,
        emoji: Annotated[str, Field(max_length=16)] = "👍",
    ) -> dict[str, Any]:
        """React to any Telegram message with an emoji from the user's personal account (pass empty string to clear). Requires explicit user approval first."""
        await backend.send_reaction(chat_id, message_id, emoji)
        return {"chat_id": chat_id, "message_id": message_id, "emoji": emoji}

    @mcp.tool()
    async def mark_read(chat_id: str, max_id: int | None = None) -> dict[str, Any]:
        """Mark a DM, group, or channel as read on Telegram itself (synced across phone/desktop) and in the local cache."""
        acked_id = await backend.mark_read(chat_id, max_id=max_id)
        return db.mark_chat_read(chat_id, max_id=acked_id if acked_id is not None else max_id)

    @mcp.tool()
    def set_chat_watch(
        chat_id: str,
        watch_mode: Literal["all", "mentions_only", "off"],
    ) -> dict[str, Any]:
        """Control which chats trigger instant MCP push notifications on tg://inbox/unread ('all', 'mentions_only', or 'off')."""
        return db.set_chat_watch(chat_id, watch_mode)

    return mcp


def build_app(db: DB, backend: TelegramBackend, provider: Provider, notifier: InboxNotifier | None = None):
    return build_mcp(db, backend, provider, notifier=notifier).streamable_http_app(host="0.0.0.0")
