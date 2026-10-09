#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BRANCH="main"
PORT="8001"
LOG_DIR="$APP_DIR/logs"
PID_FILE="$APP_DIR/.devday_web.pid"
TUNNEL_PID_FILE="$APP_DIR/.devday_tunnel.pid"
TUNNEL_URL_FILE="$APP_DIR/public_url.txt"
ACCESS_CODE_FILE="$APP_DIR/.devday_access_code"
CLOUDFLARED="/opt/homebrew/bin/cloudflared"

mkdir -p "$LOG_DIR"
cd "$APP_DIR"

# Ensure only the trusted main branch revision is used.
if [[ -n "$(git status --porcelain)" ]]; then
  echo 'Deployment checkout has local changes; commit or stash them before deploying.'
  exit 1
fi
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install -e .

# Create a local-only access code if settings.py and the environment do not provide one.
if ! "$APP_DIR/.venv/bin/python" -c 'from web import load_settings; raise SystemExit(0 if load_settings()[4] else 1)'; then
  if [[ ! -s "$ACCESS_CODE_FILE" ]]; then
    openssl rand -hex 16 > "$ACCESS_CODE_FILE"
    chmod 600 "$ACCESS_CODE_FILE"
  fi
fi
if ! "$APP_DIR/.venv/bin/python" -c 'from web import load_settings; raise SystemExit(0 if load_settings()[4] else 1)'; then
  echo 'Could not create or load the local public access code.'
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE")"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid" || true
    for _ in {1..30}; do
      kill -0 "$old_pid" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$old_pid" 2>/dev/null && kill -9 "$old_pid" || true
  fi
  rm -f "$PID_FILE"
fi

# Close any previous listener on our dedicated app port, then launch the latest code.
listener_pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$listener_pid" ]]; then
  kill $listener_pid || true
  sleep 2
fi

nohup "$APP_DIR/.venv/bin/python" "$APP_DIR/web.py" --no-browser --port "$PORT" \
  >> "$LOG_DIR/web.log" 2>&1 < /dev/null &
echo "$!" > "$PID_FILE"

for _ in {1..30}; do
  if curl --silent --fail "http://127.0.0.1:$PORT/login" >/dev/null; then
    echo "DevDay is running on port $PORT (pid $(cat "$PID_FILE"))."
    break
  fi
  sleep 1
done
if ! curl --silent --fail "http://127.0.0.1:$PORT/login" >/dev/null; then
  echo "DevDay did not become ready. Recent server log:"
  tail -n 80 "$LOG_DIR/web.log"
  exit 1
fi

if [[ ! -x "$CLOUDFLARED" ]]; then
  echo "cloudflared is missing at $CLOUDFLARED. Install it with: brew install cloudflared"
  exit 1
fi

tunnel_pid=""
if [[ -f "$TUNNEL_PID_FILE" ]]; then
  tunnel_pid="$(cat "$TUNNEL_PID_FILE")"
fi
if [[ ! "$tunnel_pid" =~ ^[0-9]+$ ]] || ! kill -0 "$tunnel_pid" 2>/dev/null; then
  rm -f "$TUNNEL_PID_FILE"
  : > "$LOG_DIR/tunnel.log"
  nohup "$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" \
    >> "$LOG_DIR/tunnel.log" 2>&1 < /dev/null &
  tunnel_pid="$!"
  echo "$tunnel_pid" > "$TUNNEL_PID_FILE"
fi

for _ in {1..30}; do
  tunnel_url="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/tunnel.log" | tail -n 1 || true)"
  if [[ -n "$tunnel_url" ]]; then
    printf '%s\n' "$tunnel_url" > "$TUNNEL_URL_FILE"
    echo "External URL: $tunnel_url"
    exit 0
  fi
  sleep 1
done

echo "Tunnel did not become ready. Recent tunnel log:"
tail -n 80 "$LOG_DIR/tunnel.log"
exit 1
