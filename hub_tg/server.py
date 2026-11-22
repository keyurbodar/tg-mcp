from typing import Annotated, Any, Literal, Protocol

from mcp.server.auth.settings import AuthSettings, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .db import DB
from .oauth import Provider


class Sender(Protocol):
    async def send_reply(self, chat_id: str, reply_to_msg_id: int, message: str) -> int: ...


ACTION_CATEGORIES = {
    "all": ["internal_ops", "partner_bd"],
    "internal_ops": ["internal_ops"],
    "partner_bd": ["partner_bd"],
    "alpha_ecosystem": ["alpha_ecosystem"],
}


def build_mcp(db: DB, sender: Sender, provider: Provider) -> MCPServer:
    mcp = MCPServer(
        name="hub-block-tg",
        instructions="Telegram triage for Hub Block. Never call send_tg_reply without the user's explicit approval of the exact text.",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=provider.issuer,
            resource_server_url=f"{provider.issuer}/mcp",
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=False,
        ),
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request):
        return PlainTextResponse("ok")

    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request):
        return await provider.login_page(request)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_pending_tg_pings(
        hours_back: Annotated[float, Field(gt=0, le=720)] = 24,
        category: Literal["all", "internal_ops", "partner_bd", "alpha_ecosystem"] = "all",
        limit: Annotated[int, Field(gt=0, le=1000)] = 400,
    ) -> dict[str, Any]:
        """Unprocessed messages from others in tracked groups, grouped by chat. 'all' means internal_ops + partner_bd."""
        groups: dict[str, dict] = {}
        rows = db.pending(hours_back, ACTION_CATEGORIES[category], limit)
        for r in rows:
            g = groups.setdefault(r["chat_id"], {
                "chat_id": r["chat_id"], "chat_title": r["chat_title"], "category": r["category"],
                "priority_level": r["priority_level"], "messages": [],
            })
            g["messages"].append({k: r[k] for k in (
                "raw_tg_id", "message_id", "sender_name", "message_text", "msg_timestamp",
                "reply_to_msg_id", "reply_to_sender", "reply_to_text")})
        return {"count": len(rows), "truncated": len(rows) == limit, "groups": list(groups.values())}

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
    async def send_tg_reply(chat_id: str, reply_to_msg_id: int, message: Annotated[str, Field(min_length=1)]) -> dict[str, Any]:
        """Post a threaded reply from the user's personal account. Requires explicit user approval of this exact text first."""
        original = db.message(f"{chat_id}:{reply_to_msg_id}")
        sent_id = await sender.send_reply(chat_id, reply_to_msg_id, message)
        db.record_sent(original, chat_id, original["chat_title"] if original else "", message)
        return {"sent_message_id": sent_id, "chat_id": chat_id, "reply_to_msg_id": reply_to_msg_id}

    @mcp.tool()
    def route_tg_to_hub_block(raw_tg_id: str, target_table: Literal["signals", "partner_briefs"], payload: dict[str, Any]) -> dict[str, Any]:
        """Save a Telegram message as a Hub Block signal or partner brief and mark it processed."""
        return {"id": db.route(raw_tg_id, target_table, payload), "target_table": target_table}

    @mcp.tool()
    def mark_tg_handled(raw_tg_ids: list[str]) -> dict[str, Any]:
        """Mark messages as processed (chatter, resolved, or replied elsewhere) so they stop showing as pending."""
        return {"updated": db.mark_processed(raw_tg_ids)}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_tg_groups() -> list[dict[str, Any]]:
        """Every group seen so far with its category, priority, active flag, and unprocessed count."""
        return db.groups()

    @mcp.tool()
    def set_tg_group(
        chat_id: str,
        category: Literal["internal_ops", "partner_bd", "alpha_ecosystem", "ignore"] | None = None,
        priority_level: Annotated[int, Field(ge=1, le=3)] | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any]:
        """Change a group's category, priority (1 high to 3 FYI), or active flag."""
        return db.set_group(chat_id, category, priority_level, is_active)

    return mcp


def build_app(db: DB, sender: Sender, provider: Provider):
    return build_mcp(db, sender, provider).streamable_http_app(host="0.0.0.0")
