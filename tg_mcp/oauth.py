import hashlib
import hmac
import html
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .db import DB

LOGIN_TTL = 600
CODE_TTL = 300
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 24 * 3600
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


class StaticClient(OAuthClientInformationFull):
    allowed_redirects: list[str] = []

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            raise InvalidRedirectUriError("redirect_uri is required")
        uri = str(redirect_uri)
        if not self.allowed_redirects or uri in self.allowed_redirects or (
            redirect_uri.scheme == "http" and redirect_uri.host in LOOPBACK_HOSTS
        ):
            return redirect_uri
        raise InvalidRedirectUriError(f"redirect_uri {uri} is not in OAUTH_REDIRECT_URIS")

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        return requested_scope.split() if requested_scope else None


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Provider:
    """OAuth 2.1 authorization server whose only login step is typing the MCP bearer token."""

    def __init__(self, db: DB, issuer: str, bearer_token: str, client_id: str, client_secret: str,
                 redirect_uris: list[str], auth_method: str):
        self.db = db
        self.issuer = issuer
        self.bearer_token = bearer_token
        self.client = StaticClient(
            client_id=client_id, client_secret=client_secret, token_endpoint_auth_method=auth_method,
            redirect_uris=None, allowed_redirects=redirect_uris,
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        )
        self.logins: dict[str, tuple[float, AuthorizationParams]] = {}
        self.codes: dict[str, AuthorizationCode] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.client if hmac.compare_digest(client_id, self.client.client_id) else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("dynamic client registration is disabled")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self.logins = {k: v for k, v in self.logins.items() if v[0] > now}
        login_id = secrets.token_urlsafe(24)
        self.logins[login_id] = (now + LOGIN_TTL, params)
        return f"{self.issuer}/login?id={login_id}"

    async def login_page(self, request: Request) -> Response:
        if request.method == "GET":
            login_id = request.query_params.get("id", "")
            return self._form(login_id, None) if login_id in self.logins else self._expired()
        form = await request.form()
        login_id = str(form.get("id", ""))
        entry = self.logins.get(login_id)
        if entry is None or entry[0] < time.time():
            return self._expired()
        if not hmac.compare_digest(str(form.get("token", "")).strip().encode(), self.bearer_token.encode()):
            return self._form(login_id, "Wrong token.")
        del self.logins[login_id]
        params = entry[1]
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + CODE_TTL,
            client_id=self.client.client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 302)

    def _form(self, login_id: str, error: str | None) -> HTMLResponse:
        message = f'<p class="err">{html.escape(error)}</p>' if error else ""
        return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>tg-mcp login</title>
<style>body{{font:15px system-ui,sans-serif;max-width:360px;margin:15vh auto;padding:0 16px}}
input,button{{width:100%;box-sizing:border-box;padding:10px;margin-top:8px;font:inherit}}.err{{color:#b00}}</style></head>
<body><h2>tg-mcp</h2><p>Paste the MCP bearer token to authorize this client.</p>{message}
<form method="post" action="{self.issuer}/login"><input type="hidden" name="id" value="{html.escape(login_id)}">
<input type="password" name="token" placeholder="MCP_BEARER_TOKEN" autofocus required>
<button type="submit">Authorize</button></form></body></html>""", status_code=401 if error else 200)

    def _expired(self) -> HTMLResponse:
        return HTMLResponse("<p>This login link expired. Start the connection again from your client.</p>", status_code=400)

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        row = self.db.oauth_token(digest(refresh_token), "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"].split(),
                            expires_at=row["expires_at"], resource=row["resource"])

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]):
        self.db.delete_oauth_token(digest(refresh_token.token))
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self.bearer_token.encode()):
            return AccessToken(token=token, client_id="static-bearer", scopes=[])
        row = self.db.oauth_token(digest(token), "access")
        if row is None:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                           expires_at=row["expires_at"], resource=row["resource"])

    async def revoke_token(self, token) -> None:
        self.db.delete_oauth_token(digest(token.token))

    def _issue(self, client_id: str, scopes: list[str], resource: str | None):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        self.db.save_oauth_token(digest(access), "access", client_id, " ".join(scopes), resource, now + ACCESS_TTL)
        self.db.save_oauth_token(digest(refresh), "refresh", client_id, " ".join(scopes), resource, now + REFRESH_TTL)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes) or None)
