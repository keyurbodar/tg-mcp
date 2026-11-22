import asyncio
import base64
import hashlib
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from hub_tg.db import DB
from hub_tg.oauth import Provider
from hub_tg.server import PENDING_PINGS_URI, PingNotifier, build_app

TOKEN = "test-token"
CLIENT_ID = "gemini"
CLIENT_SECRET = "s3cret"
REDIRECT = "http://localhost:7777/oauth/callback"


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send_reply(self, chat_id, reply_to_msg_id, message):
        self.sent.append((chat_id, reply_to_msg_id, message))
        return 999


def save(db, chat_id, message_id, text, *, from_me=False, reply_to=None, hours_ago=1, title="Ops"):
    return db.save_message(chat_id=chat_id, chat_title=title, message_id=message_id, sender_id=1,
                           sender_name="me" if from_me else "alice", message_text=text, reply_to_msg_id=reply_to,
                           from_me=from_me, msg_timestamp=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                           default_category="internal_ops")


@pytest.fixture
def server(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    sender = FakeSender()
    notifier = PingNotifier()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    provider = Provider(db, f"http://127.0.0.1:{port}", TOKEN, CLIENT_ID, CLIENT_SECRET, [], "client_secret_post")
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    srv = uvicorn.Server(uvicorn.Config(build_app(db, sender, provider, notifier=notifier), host="127.0.0.1", port=port, log_level="warning"))

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder["loop"] = loop
        loop.run_until_complete(srv.serve())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", db, sender, notifier, loop_holder["loop"]
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


def test_triage_flow(server):
    url, db, sender, *_ = server
    assert save(db, "-100", 1, "can you review the deck?") is True
    assert save(db, "-100", 1, "can you review the deck?") is False
    save(db, "-100", 2, "old", hours_ago=200)
    assert save(db, "-100", 3, "my own msg", from_me=True) is False
    save(db, "-200", 10, "replied already in app")
    save(db, "-200", 11, "on it", from_me=True, reply_to=10)
    save(db, "-300", 20, "partner update", title="Partner X")
    asyncio.run(call(url, "set_tg_group", {"chat_id": "-300", "category": "partner_bd"}))

    pending = asyncio.run(call(url, "get_pending_tg_pings", {"hours_back": 120}))
    ids = [m["raw_tg_id"] for g in pending["groups"] for m in g["messages"]]
    assert sorted(ids) == ["-100:1", "-300:20"]
    only_ops = asyncio.run(call(url, "get_pending_tg_pings", {"category": "internal_ops"}))
    assert [g["chat_id"] for g in only_ops["groups"]] == ["-100"]

    sent = asyncio.run(call(url, "send_tg_reply", {"chat_id": "-100", "reply_to_msg_id": 1, "message": "lgtm"}))
    assert sent["sent_message_id"] == 999
    assert sender.sent == [("-100", 1, "lgtm")]

    routed = asyncio.run(call(url, "route_tg_to_hub_block", {"raw_tg_id": "-300:20", "target_table": "partner_briefs", "payload": {"note": "x"}}))
    assert routed["target_table"] == "partner_briefs"

    assert asyncio.run(call(url, "get_pending_tg_pings", {"hours_back": 120}))["count"] == 0
    assert db.conn.execute("SELECT status FROM tg_action_items").fetchone()[0] == "sent"


def test_resource_subscription_and_push_notification(server):
    url, db, _, notifier, srv_loop = server

    async def run_client():
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client:
                assert client.server_capabilities.resources is not None
                assert client.server_capabilities.resources.subscribe is True

                resources = await client.list_resources()
                assert any(str(r.uri) == PENDING_PINGS_URI for r in resources.resources)

                async with client.listen(resource_subscriptions=[PENDING_PINGS_URI]) as sub:
                    assert save(db, "-500", 42, "@keyur need quick update on ixs") is True
                    fut = asyncio.run_coroutine_threadsafe(
                        notifier.notify_resource_updated(PENDING_PINGS_URI), srv_loop
                    )
                    fut.result(timeout=5)

                    async def next_event():
                        async for ev in sub:
                            return ev

                    ev = await asyncio.wait_for(next_event(), timeout=5)
                    assert str(ev.uri) == PENDING_PINGS_URI

                res = await client.read_resource(PENDING_PINGS_URI)
                payload = json.loads(res.contents[0].text)
                assert payload["count"] == 1
                assert payload["groups"][0]["messages"][0]["message_text"] == "@keyur need quick update on ixs"

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
    assert asyncio.run(call(url, "get_pending_tg_pings", {}, token=tokens["access_token"]))["count"] == 0
    assert httpx.post(meta["token_endpoint"], data={**token_form, "client_secret": CLIENT_SECRET}).status_code == 400

    refreshed = httpx.post(meta["token_endpoint"], data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}).json()
    assert asyncio.run(call(url, "get_pending_tg_pings", {}, token=refreshed["access_token"]))["count"] == 0
