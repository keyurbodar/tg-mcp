# hub-block-tg

Telegram ingestion and triage for Hub Block. It runs as your personal Telegram account (MTProto via Telethon), silently logs every group and channel message into SQLite, and serves a Bearer-protected remote MCP endpoint so Claude Code or Cursor can pull pending pings, draft replies, and post threaded replies after you approve them.

No bots are added and no group settings change. Messages are never marked as read.

## How it runs

One container, one process. The Telegram listener and the MCP server share a single Telethon client, because two processes using the same session string at once makes Telegram revoke it (`AUTH_KEY_DUPLICATED`).

- `hub_tg/worker.py`: connects to Telegram, stores new messages, backfills the last `BACKFILL_HOURS` (default 120) on every start, and serves HTTP on port 8080.
- `hub_tg/server.py`: the MCP tools and auth. `/mcp` accepts the static bearer token or an OAuth access token. `/healthz`, `/login`, and the OAuth endpoints are public.
- `hub_tg/oauth.py`: a small OAuth 2.1 server (fixed client id and secret, PKCE, refresh tokens) whose login page only asks for the bearer token.
- `hub_tg/db.py`: SQLite schema (`raw_tg_staging`, `tg_group_configs`, `tg_action_items`, `signals`, `partner_briefs`) at `./data/hub_tg.db`.

## Deploy on a VPS

Needs Docker with the compose plugin, `git`, and `openssl`. Get `api_id` and `api_hash` from https://my.telegram.org under "API development tools".

```bash
git clone https://github.com/keyurbodar/hub-block-tg.git && cd hub-block-tg
TELEGRAM_API_ID=123456 TELEGRAM_API_HASH=abcdef DOMAIN=tg.example.com ./setup.sh init
./setup.sh login     # interactive: phone number, login code, 2FA password if set
./setup.sh up
./setup.sh token     # the Bearer token for your MCP client
```

`./setup.sh login` needs a person, because Telegram sends the login code to your app. Run it once; the session string is stored in `.env` (mode 600) and survives restarts. Treat it like a password: it is full access to your account.

### HTTPS

Pick one:

- **Bundled Caddy.** Set `DOMAIN` and point its DNS A record at the VPS. `./setup.sh up` then starts Caddy on 80/443 with an automatic certificate, and the endpoint is `https://DOMAIN/mcp`.
- **Your own reverse proxy.** Leave `DOMAIN` empty and set `PUBLIC_URL=https://your-host` in `.env` (OAuth needs it). The app listens on `127.0.0.1:8080`; proxy all paths of `https://your-host` to `http://127.0.0.1:8080`, not only `/mcp`. Disable response buffering so streaming works (nginx: `proxy_buffering off;`).

Setting `MCP_BIND=0.0.0.0` exposes plain HTTP. The token then crosses the network unencrypted, so use it only inside a private network such as Tailscale.

### Operate

```bash
docker compose logs -f tg               # "telegram connected as ..." and "backfill stored ..."
curl -s localhost:8080/healthz          # ok
git pull && ./setup.sh init && ./setup.sh up   # update (init fills in new .env keys)
```

Data lives in `./data/hub_tg.db`. Back up that file and `.env`.

## Connect Claude Code or Cursor

```bash
claude mcp add --transport http vps-tg-ops https://tg.example.com/mcp --header "Authorization: Bearer $TOKEN"
```

Or copy [`claude/mcp.json.example`](claude/mcp.json.example) into `~/.cursor/mcp.json` or `.mcp.json`. Copy [`claude/commands/tg-clear.md`](claude/commands/tg-clear.md) to `.claude/commands/` to get `/tg-clear`.

### Gemini and other OAuth-only clients

`./setup.sh oauth` prints the client id, client secret, and endpoint URLs. For Gemini CLI, add this to `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "vps-tg-ops": {
      "httpUrl": "https://tg.example.com/mcp",
      "oauth": {
        "enabled": true,
        "clientId": "OAUTH_CLIENT_ID",
        "clientSecret": "OAUTH_CLIENT_SECRET",
        "authorizationUrl": "https://tg.example.com/authorize",
        "tokenUrl": "https://tg.example.com/token"
      }
    }
  }
}
```

Then run `/mcp auth vps-tg-ops` in Gemini. The browser opens a page that asks for the bearer token (`./setup.sh token`). Paste it and Gemini gets its own tokens: access tokens last 24 hours and refresh automatically for 90 days.

Clients that discover OAuth on their own can use just the MCP URL, client id, and client secret. The server publishes `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`. Dynamic client registration is off. Loopback redirect URIs always work; set `OAUTH_REDIRECT_URIS` to limit the others. If a client sends its secret with HTTP Basic auth, set `OAUTH_TOKEN_AUTH_METHOD=client_secret_basic`.

## Tools

| Tool | What it does |
| --- | --- |
| `get_pending_tg_pings` | Unprocessed messages from others, grouped by chat. `hours_back` (default 24), `category` (`all` = internal_ops + partner_bd, or one category), `limit`. Each message carries `raw_tg_id`, `message_id`, and the text it replies to. |
| `send_tg_reply` | Threaded reply from your account (`chat_id`, `reply_to_msg_id`, `message`). Marks the original handled and logs it in `tg_action_items`. Marked destructive, so clients ask before calling it. |
| `route_tg_to_hub_block` | Saves a message into `signals` or `partner_briefs` with a JSON `payload`, and marks it handled. |
| `mark_tg_handled` | Clears chatter or resolved items by `raw_tg_id`. |
| `list_tg_groups` | Every group seen, with category, priority, and unprocessed count. |
| `set_tg_group` | Sets a group's `category` (`internal_ops`, `partner_bd`, `alpha_ecosystem`, `ignore`), `priority_level` (1 to 3), or `is_active`. |

New groups start as `TG_DEFAULT_CATEGORY` (default `internal_ops`) and broadcast channels as `alpha_ecosystem`. Run `list_tg_groups` after the first backfill and set noisy groups to `ignore`.

A message counts as handled when you reply through `send_tg_reply`, when you reply to it yourself in Telegram, or when a tool marks it. Your own messages never show as pending.

## Develop

```bash
uv venv && uv pip install -e '.[test]' && .venv/bin/pytest -q
```

The tests drive the real MCP endpoint over HTTP with a fake Telegram sender, including the full OAuth flow.
