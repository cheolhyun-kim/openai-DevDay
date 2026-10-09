#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BRANCH="main"
PORT="8001"
LOG_DIR="$APP_DIR/logs"
TUNNEL_URL_FILE="$APP_DIR/public_url.txt"
ACCESS_CODE_FILE="$APP_DIR/.devday_access_code"
CLOUDFLARED="/opt/homebrew/bin/cloudflared"
SERVICE_USER="kimcheolhyun"
SERVICE_UID="$(id -u "$SERVICE_USER")"
LAUNCH_AGENTS="/Users/$SERVICE_USER/Library/LaunchAgents"
LAUNCH_DOMAIN="gui/$SERVICE_UID"
WEB_LABEL="com.devday.web"
TUNNEL_LABEL="com.devday.tunnel"

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

mkdir -p "$LAUNCH_AGENTS"
cp "$APP_DIR/launchd/$WEB_LABEL.plist" "$LAUNCH_AGENTS/$WEB_LABEL.plist"
cp "$APP_DIR/launchd/$TUNNEL_LABEL.plist" "$LAUNCH_AGENTS/$TUNNEL_LABEL.plist"

# Keep the server outside the Actions job process. A loaded LaunchAgent is restarted
# in place; bootout/bootstrap on every push can race launchd and fail with error 5.
WEB_SERVICE="$LAUNCH_DOMAIN/$WEB_LABEL"
WEB_PLIST="$LAUNCH_AGENTS/$WEB_LABEL.plist"
if launchctl print "$WEB_SERVICE" >/dev/null 2>&1; then
  launchctl kickstart -k "$WEB_SERVICE"
else
  if ! launchctl bootstrap "$LAUNCH_DOMAIN" "$WEB_PLIST"; then
    # Compatibility fallback for macOS launchctl contexts that reject bootstrap.
    if ! launchctl load -w "$WEB_PLIST"; then
      echo "Could not load $WEB_LABEL in $LAUNCH_DOMAIN. Run this once from Terminal while signed in:"
      echo "launchctl bootstrap $LAUNCH_DOMAIN $WEB_PLIST"
      exit 1
    fi
  fi
  launchctl kickstart -k "$WEB_SERVICE"
fi

for _ in {1..30}; do
  if curl --silent --fail "http://127.0.0.1:$PORT/login" >/dev/null; then
    echo "DevDay is running on port $PORT under launchd."
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

if ! launchctl print "$LAUNCH_DOMAIN/$TUNNEL_LABEL" >/dev/null 2>&1; then
  : > "$LOG_DIR/tunnel.log"
  : > "$LOG_DIR/tunnel.error.log"
  if ! launchctl bootstrap "$LAUNCH_DOMAIN" "$LAUNCH_AGENTS/$TUNNEL_LABEL.plist"; then
    if ! launchctl load -w "$LAUNCH_AGENTS/$TUNNEL_LABEL.plist"; then
      echo "Could not load $TUNNEL_LABEL in $LAUNCH_DOMAIN."
      exit 1
    fi
  fi
fi

for _ in {1..30}; do
  tunnel_url="$(grep -Eho 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/tunnel.log" "$LOG_DIR/tunnel.error.log" 2>/dev/null | tail -n 1 || true)"
  if [[ -n "$tunnel_url" ]]; then
    printf '%s\n' "$tunnel_url" > "$TUNNEL_URL_FILE"
    echo "External URL: $tunnel_url"
    exit 0
  fi
  sleep 1
done

echo "Tunnel did not become ready. Recent tunnel log:"
tail -n 80 "$LOG_DIR/tunnel.log"
tail -n 80 "$LOG_DIR/tunnel.error.log"
exit 1
