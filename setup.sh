#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

set_env() {
  local key=$1 value=$2
  if grep -q "^$key=" .env; then
    python3 - "$key" "$value" <<'PY'
import sys
key, value = sys.argv[1:]
lines = open(".env").read().splitlines()
out = [f"{key}={value}" if l.startswith(f"{key}=") else l for l in lines]
open(".env", "w").write("\n".join(out) + "\n")
PY
  else
    echo "$key=$value" >> .env
  fi
}

get_env() { grep "^$1=" .env | head -1 | cut -d= -f2- || true; }

init() {
  [ -f .env ] || { cp .env.example .env; chmod 600 .env; }
  mkdir -p data && chown 1000:1000 data 2>/dev/null || chmod 777 data
  [ -n "$(get_env MCP_BEARER_TOKEN)" ] || set_env MCP_BEARER_TOKEN "$(openssl rand -hex 32)"
  [ -n "$(get_env OAUTH_CLIENT_ID)" ] || set_env OAUTH_CLIENT_ID "hub-block-tg-$(openssl rand -hex 4)"
  [ -n "$(get_env OAUTH_CLIENT_SECRET)" ] || set_env OAUTH_CLIENT_SECRET "$(openssl rand -hex 32)"
  for key in TELEGRAM_API_ID TELEGRAM_API_HASH DOMAIN; do
    if [ -z "$(get_env $key)" ] && [ -n "${!key:-}" ]; then set_env "$key" "${!key}"; fi
  done
  docker compose build
  echo "init done. next: ./setup.sh login  (interactive, needs the Telegram code)"
}

login() {
  [ -n "$(get_env TELEGRAM_API_ID)" ] && [ -n "$(get_env TELEGRAM_API_HASH)" ] \
    || { echo "set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first"; exit 1; }
  rm -f data/.session
  docker compose run --rm tg python -m hub_tg.login --out /data/.session
  set_env TELEGRAM_SESSION_STRING "$(cat data/.session)"
  rm -f data/.session
  echo "session saved to .env. next: ./setup.sh up"
}

up() {
  if [ -n "$(get_env DOMAIN)" ]; then
    docker compose --profile https up -d --build
    echo "MCP endpoint: https://$(get_env DOMAIN)/mcp"
  else
    docker compose up -d --build
    echo "MCP endpoint: http://$(get_env MCP_BIND):$(get_env MCP_PORT)/mcp (put a TLS reverse proxy in front)"
  fi
}

token() { get_env MCP_BEARER_TOKEN; }

oauth() {
  local base
  base=$(get_env PUBLIC_URL)
  [ -n "$base" ] || base="https://$(get_env DOMAIN)"
  echo "client_id:     $(get_env OAUTH_CLIENT_ID)"
  echo "client_secret: $(get_env OAUTH_CLIENT_SECRET)"
  echo "mcp url:       $base/mcp"
  echo "authorize url: $base/authorize"
  echo "token url:     $base/token"
  echo "login asks for the bearer token: ./setup.sh token"
}

case "${1:-}" in
  init|login|up|token|oauth) "$1" ;;
  *) echo "usage: ./setup.sh init|login|up|token|oauth"; exit 1 ;;
esac
