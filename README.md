# tg-mcp

Universal personal Telegram MCP server. Runs as your personal Telegram account (MTProto via Telethon), ingests **DMs, groups, and channels** into SQLite with live Telegram read/unread state sync, and serves a Bearer + OAuth 2.1 remote MCP endpoint with real-time `notifications/resources/updated` push subscriptions.

## Tools & Resources

| Tool / Resource | What it does |
| --- | --- |
| `tg://inbox/unread` (resource) | Subscribable MCP resource (`subscriptions/listen` and `resources/subscribe`) that pushes `notifications/resources/updated` when new unread messages arrive in watched chats. |
| `list_chats` | Lists DMs, groups, and channels (`chat_type`, `unread_only`, `limit`) with live unread counts, mention counts, and last message preview. |
| `get_unread` | Returns actually-unread messages grouped by chat (synced with Telegram's `read_inbox_max_id`). Supports `chat_type`, `mentions_only`, `hours_back`, `limit`. |
| `get_messages` | Reads recent messages in any DM, group, or channel in chronological order (`chat_id`, `limit`, `before_id`). |
| `search_messages` | Searches across all chats or within one `chat_id`. |
| `send_message` | Sends a message or threaded reply (`chat_id`, `message`, `reply_to_msg_id?`). Destructive/approval-gated. |
| `react_message` | Adds or clears an emoji reaction (`chat_id`, `message_id`, `emoji`, empty string clears). Destructive/approval-gated. |
| `mark_read` | Marks a chat read on Telegram itself (`send_read_acknowledge`, synced across phone/desktop) and updates local state. |
| `set_chat_watch` | Sets live push filter per chat: `all` (default for DMs/groups), `mentions_only`, or `off` (default for channels). |

## Deploy / Update on a VPS

```bash
git pull && docker compose up -d --build
```

Fresh setup:

```bash
git clone https://github.com/keyurbodar/tg-mcp.git && cd tg-mcp
TELEGRAM_API_ID=123456 TELEGRAM_API_HASH=abcdef DOMAIN=tg.example.com ./setup.sh init
./setup.sh login
./setup.sh up
./setup.sh token
```

## Develop

```bash
pytest -v
```
