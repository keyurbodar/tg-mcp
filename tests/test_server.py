import asyncio
import base64
import hashlib
import json
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from tg_mcp.db import DB
from tg_mcp.oauth import Provider
from tg_mcp.server import UNREAD_INBOX_URI, InboxNotifier, build_app

TOKEN = "test-token"
CLIENT_ID = "gemini"
CLIENT_SECRET = "s3cret"
REDIRECT = "http://localhost:7777/oauth/callback"


class FakeBackend:
    def __init__(self):
        self.sent = []
        self.reactions = []
        self.marked_read = []
        self.live_fetched = []

    async def send_message(self, chat_id, message, reply_to_msg_id=None):
        self.sent.append((chat_id, message, reply_to_msg_id))
        return 999

    async def send_reaction(self, chat_id, message_id, emoji):
        self.reactions.append((chat_id, message_id, emoji))

    async def mark_read(self, chat_id, max_id=None):
        self.marked_read.append((chat_id, max_id))
        return max_id

    async def fetch_messages(self, chat_id, limit=50, before_id=None):
        self.live_fetched.append((chat_id, limit, before_id))
        return [{"msg_uid": f"{chat_id}:1", "chat_id": chat_id, "message_id": 1, "sender_name": "live", "message_text": "fetched live", "from_me": False}]


def save(
    db: DB,
    chat_id: str,
    message_id: int,
    text: str,
    *,
    chat_type: str = "group",
    title: str = "Team Chat",
    from_me: bool = False,
    is_mention: bool = False,
    reply_to: int | None = None,
    hours_ago: float = 1,
):
    return db.save_message(
        chat_id=chat_id,
        title=title,
        username=None,
        chat_type=chat_type,
        message_id=message_id,
        sender_id=1,
        sender_name="me" if from_me else "alice",
        message_text=text,
        reply_to_msg_id=reply_to,
        from_me=from_me,
        is_mention=is_mention,
        msg_timestamp=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


@pytest.fixture
def server(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    backend = FakeBackend()
    notifier = InboxNotifier()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    provider = Provider(db, f"http://127.0.0.1:{port}", TOKEN, CLIENT_ID, CLIENT_SECRET, [], "client_secret_post")
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    srv = uvicorn.Server(uvicorn.Config(build_app(db, backend, provider, notifier=notifier), host="127.0.0.1", port=port, log_level="warning"))

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder["loop"] = loop
        loop.run_until_complete(srv.serve())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", db, backend, notifier, loop_holder["loop"]
    srv.should_exit = True
    thread.join()


async def call(url, tool, args, token=TOKEN):
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(f"{url}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                assert not result.is_error, result.content
                return json.loads(result.content[0].text)


def test_rejects_missing_or_wrong_token(server):
    url, *_ = server
    unauthenticated = httpx.post(f"{url}/mcp", json={})
    assert unauthenticated.status_code == 401
    assert "/.well-known/oauth-protected-resource" in unauthenticated.headers["www-authenticate"]
    assert httpx.post(f"{url}/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert httpx.get(f"{url}/healthz").status_code == 200


def test_bots_never_hide_human_dms_and_per_chat_capping(server):
    url, db, backend, *_ = server
    # Spam bot with 25 unread messages (must NOT push notify and must NOT hide human DMs)
    for i in range(1, 26):
        assert save(db, "70001", i, f"promo spam #{i}", chat_type="bot", title="Spam OTP Bot", hours_ago=0.5) is False

    # Older human DM from ndx (100h ago — previously missed by 72h cutoff and bot overflow)
    assert save(db, "42001", 1, "hey keyur, quick question", chat_type="dm", title="ndx", hours_ago=100) is True
    # Group chatter vs mention
    assert save(db, "-1001", 10, "gm everyone", chat_type="group", title="Core Team") is True
    assert save(db, "-1001", 11, "@keyur can you check this?", chat_type="group", title="Core Team", is_mention=True) is True
    # Archived chat excluded by default
    save(db, "42099", 1, "archived dm", chat_type="dm", title="Old Contact")
    db.upsert_chat(chat_id="42099", title="Old Contact", chat_type="dm", is_archived=True)

    # Default get_unread excludes bots & archived, includes older human DM from ndx
    unread = asyncio.run(call(url, "get_unread", {}))
    chat_ids = [c["chat_id"] for c in unread["chats"]]
    assert "42001" in chat_ids
    assert "-1001" in chat_ids
    assert "70001" not in chat_ids
    assert "42099" not in chat_ids

    # Explicitly asking for bots caps per_chat_limit so bots never flood payload
    bots_unread = asyncio.run(call(url, "get_unread", {"chat_type": "bot", "per_chat_limit": 5}))
    assert bots_unread["chat_count"] == 1
    assert bots_unread["chats"][0]["unread_count"] == 25
    assert len(bots_unread["chats"][0]["messages"]) == 5
    assert bots_unread["chats"][0]["messages_truncated"] is True

    # mentions_only returns all unread human DMs + group mentions/replies
    important = asyncio.run(call(url, "get_unread", {"mentions_only": True}))
    uids = [m["msg_uid"] for c in important["chats"] for m in c["messages"]]
    assert uids == ["42001:1", "-1001:11"]

    # list_chats filters
    dms = asyncio.run(call(url, "list_chats", {"chat_type": "dm", "unread_only": True}))["chats"]
    assert [c["chat_id"] for c in dms] == ["42001"]

    # get_messages cached vs live fallback
    hist = asyncio.run(call(url, "get_messages", {"chat_id": "-1001"}))["messages"]
    assert [m["message_id"] for m in hist] == [10, 11]
    live_hist = asyncio.run(call(url, "get_messages", {"chat_id": "88888"}))["messages"]
    assert live_hist[0]["message_text"] == "fetched live"
    assert backend.live_fetched == [("88888", 50, None)]

    # search_messages, send_message, react_message, mark_read, set_chat_watch
    hits = asyncio.run(call(url, "search_messages", {"query": "quick question"}))["messages"]
    assert len(hits) == 1 and hits[0]["chat_id"] == "42001"

    sent_dm = asyncio.run(call(url, "send_message", {"chat_id": "42001", "message": "hey whats up"}))
    assert sent_dm == {"sent_message_id": 999, "chat_id": "42001", "reply_to_msg_id": None}
    reacted = asyncio.run(call(url, "react_message", {"chat_id": "-1001", "message_id": 11, "emoji": "🔥"}))
    assert reacted == {"chat_id": "-1001", "message_id": 11, "emoji": "🔥"}

    read_res = asyncio.run(call(url, "mark_read", {"chat_id": "-1001"}))
    assert read_res["unread_count"] == 0
    assert read_res["read_inbox_max_id"] == 11

    watched = asyncio.run(call(url, "set_chat_watch", {"chat_id": "-1001", "watch_mode": "mentions_only"}))
    assert watched["watch_mode"] == "mentions_only"
    assert save(db, "-1001", 12, "random chatter", chat_type="group", title="Core Team") is False
    assert save(db, "-1001", 13, "@keyur ping again", chat_type="group", title="Core Team", is_mention=True) is True


def test_auto_migrates_old_chats_table(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE chats (
            chat_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            chat_type TEXT NOT NULL CHECK (chat_type IN ('dm', 'group', 'channel')),
            read_inbox_max_id INTEGER NOT NULL DEFAULT 0,
            unread_count INTEGER NOT NULL DEFAULT 0,
            unread_mentions_count INTEGER NOT NULL DEFAULT 0,
            watch_mode TEXT NOT NULL DEFAULT 'all',
            last_message_id INTEGER NOT NULL DEFAULT 0,
            last_message_at TIMESTAMP,
            last_message_preview TEXT
        );
        INSERT INTO chats (chat_id, title, chat_type) VALUES ('1', 'OldDM', 'dm');
    """)
    conn.close()
    db = DB(db_path)
    db.upsert_chat(chat_id="2", title="SomeBot", chat_type="bot", is_archived=False, is_muted=True)
    bots = db.list_chats(chat_type="bot")
    assert len(bots) == 1 and bots[0]["title"] == "SomeBot" and bots[0]["is_muted"] == 1


def test_live_unread_subscription_and_push(server):
    url, db, _, notifier, srv_loop = server

    async def run_client():
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client:
                assert client.server_capabilities.resources is not None
                assert client.server_capabilities.resources.subscribe is True

                resources = await client.list_resources()
                assert any(str(r.uri) == UNREAD_INBOX_URI for r in resources.resources)

                async with client.listen(resource_subscriptions=[UNREAD_INBOX_URI]) as sub:
                    assert save(db, "99001", 5, "yo call me when free", chat_type="dm", title="Alex") is True
                    fut = asyncio.run_coroutine_threadsafe(
                        notifier.notify_resource_updated(UNREAD_INBOX_URI), srv_loop
                    )
                    fut.result(timeout=5)

                    async def next_event():
                        async for ev in sub:
                            return ev

                    ev = await asyncio.wait_for(next_event(), timeout=5)
                    assert str(ev.uri) == UNREAD_INBOX_URI

                res = await client.read_resource(UNREAD_INBOX_URI)
                payload = json.loads(res.contents[0].text)
                assert payload["chat_count"] == 1
                assert payload["chats"][0]["chat_type"] == "dm"
                assert payload["chats"][0]["messages"][0]["message_text"] == "yo call me when free"

    asyncio.run(run_client())


def test_oauth_flow(server):
    url, *_ = server
    resource = httpx.get(f"{url}/.well-known/oauth-protected-resource/mcp").json()
    assert resource["authorization_servers"][0].rstrip("/") == url
    meta = httpx.get(f"{url}/.well-known/oauth-authorization-server").json()

    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth = httpx.get(meta["authorization_endpoint"], params={
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "state": "xyz",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "mcp",
    })
    assert auth.status_code == 302, auth.text
    login_url = auth.headers["location"]
    assert login_url.startswith(f"{url}/login?id=")
    login_id = login_url.split("id=")[1]
    assert "MCP_BEARER_TOKEN" in httpx.get(login_url).text

    assert httpx.post(f"{url}/login", data={"id": login_id, "token": "wrong"}).status_code == 401
    done = httpx.post(f"{url}/login", data={"id": login_id, "token": TOKEN})
    assert done.status_code == 302
    callback = httpx.URL(done.headers["location"])
    assert str(callback).startswith(REDIRECT) and callback.params["state"] == "xyz"

    token_form = {"grant_type": "authorization_code", "code": callback.params["code"], "redirect_uri": REDIRECT,
                  "client_id": CLIENT_ID, "code_verifier": verifier}
    assert httpx.post(meta["token_endpoint"], data={**token_form, "client_secret": "nope"}).status_code == 401
    tokens = httpx.post(meta["token_endpoint"], data={**token_form, "client_secret": CLIENT_SECRET}).json()
    assert asyncio.run(call(url, "get_unread", {}, token=tokens["access_token"]))["chat_count"] == 0
    assert httpx.post(meta["token_endpoint"], data={**token_form, "client_secret": CLIENT_SECRET}).status_code == 400

    refreshed = httpx.post(meta["token_endpoint"], data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}).json()
    assert asyncio.run(call(url, "get_unread", {}, token=refreshed["access_token"]))["chat_count"] == 0
