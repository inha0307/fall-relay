#!/usr/bin/env bash
# Install the server-side user services: the Fall-Relay server (bridge API), the web application, and one
# reverse SSH tunnel per edge device listed in RPI_TARGETS.
#
# Settings come from deploy.env next to this script (copy deploy.env.example), and may be
# overridden by environment variables of the same name.
#
#   bash install_server_services.sh             build the web application, then install and start the units
#   bash install_server_services.sh --no-build  keep the existing build in frontend/.next
set -euo pipefail

BUILD=1
case "${1-}" in
  "") ;;
  --no-build) BUILD=0 ;;
  *) echo "usage: $0 [--no-build]" >&2; exit 2 ;;
esac

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$APP_DIR/deploy.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$APP_DIR/deploy.env"; set +a
fi

SERVER_ROOT="${FALLRELAY_SERVER_ROOT:-}"
[[ -n "$SERVER_ROOT" ]] || { echo "Set FALLRELAY_SERVER_ROOT in deploy.env (see deploy.env.example)." >&2; exit 1; }
BRIDGE_CONFIG="${FALLRELAY_SERVER_CONFIG:-server_config.json}"
[[ "$BRIDGE_CONFIG" = /* ]] || BRIDGE_CONFIG="$APP_DIR/$BRIDGE_CONFIG"
RPI_TARGETS="${RPI_TARGETS-}"
SSH_KEY="${RPI_SSH_KEY-}"
TUNNEL_PORT="${RPI_TUNNEL_PORT:-15001}"
SERVER_PYTHON="$SERVER_ROOT/.venv/bin/python"
FRONTEND_DIR="$SERVER_ROOT/frontend"
NPM_BIN="$(command -v npm || true)"
SSH_BIN="$(command -v ssh || true)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
API_UNIT=fall-relay-server.service
WEB_UNIT=fall-relay-web.service

for path in "$APP_DIR/video_bridge.py" "$APP_DIR/server_config.py" "$APP_DIR/hardware_store.py" \
            "$APP_DIR/device_profiles.py" "$APP_DIR/model_contract.py" "$APP_DIR/model_run_store.py" \
            "$BRIDGE_CONFIG" "$SERVER_PYTHON" "$FRONTEND_DIR/package.json" "$FRONTEND_DIR/package-lock.json"; do
  [[ -e "$path" ]] || { echo "Required path not found: $path" >&2; exit 1; }
done
[[ -n "$NPM_BIN" ]] || { echo "npm is required (Node.js LTS)." >&2; exit 1; }
case "$APP_DIR$SERVER_ROOT$BRIDGE_CONFIG$SSH_KEY" in *[[:space:]]*) echo "Service paths with spaces are not supported." >&2; exit 1 ;; esac

BRIDGE_PORT="$(cd "$APP_DIR" && "$SERVER_PYTHON" -c 'import sys,fastapi,uvicorn; from server_config import load_server_config; c=load_server_config(sys.argv[1]); print(c["port"])' "$BRIDGE_CONFIG")"
if [[ -z "${FALLRELAY_WRITE_TOKEN:-}" ]]; then
  echo "WARNING: FALLRELAY_WRITE_TOKEN is empty; the web application cannot reach the server API (see deploy.env.example)." >&2
fi
echo "Bridge config OK: $BRIDGE_CONFIG (port $BRIDGE_PORT)"

if [[ -n "$RPI_TARGETS" ]]; then
  [[ -n "$SSH_BIN" ]] || { echo "ssh client is required for RPI_TARGETS tunnels." >&2; exit 1; }
  [[ -n "$SSH_KEY" ]] || { echo "Set RPI_SSH_KEY in deploy.env for the RPI_TARGETS tunnels." >&2; exit 1; }
  [[ -r "$SSH_KEY" ]] || { echo "Readable Raspberry Pi SSH key not found: $SSH_KEY" >&2; exit 1; }
fi

if [[ "$BUILD" == 1 ]]; then
  if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    (cd "$FRONTEND_DIR" && "$NPM_BIN" ci)
  fi
  (cd "$FRONTEND_DIR" && "$NPM_BIN" run build)
else
  [[ -f "$FRONTEND_DIR/.next/BUILD_ID" ]] || { echo "--no-build needs an existing build in $FRONTEND_DIR/.next" >&2; exit 1; }
fi

mkdir -p "$UNIT_DIR"
stamp="$(date +%Y%m%d%H%M%S)"
backup() { if [[ -f "$UNIT_DIR/$1" ]]; then cp -a "$UNIT_DIR/$1" "$UNIT_DIR/$1.backup.$stamp"; fi; }

backup "$API_UNIT"
cat > "$UNIT_DIR/$API_UNIT" <<EOF
[Unit]
Description=Fall-Relay server (edge results, events, alerts and device API)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
# deploy.env supplies FALLRELAY_WRITE_TOKEN and, for PostgreSQL, FALLRELAY_DATABASE_URL; keep that file private.
EnvironmentFile=-$APP_DIR/deploy.env
ExecStart=$SERVER_PYTHON $APP_DIR/video_bridge.py --config $BRIDGE_CONFIG
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

backup "$WEB_UNIT"
cat > "$UNIT_DIR/$WEB_UNIT" <<EOF
[Unit]
Description=Fall-Relay web application
After=$API_UNIT
Wants=$API_UNIT

[Service]
Type=simple
WorkingDirectory=$FRONTEND_DIR
# FALLRELAY_WRITE_TOKEN and FALLRELAY_DATABASE_URL come from the same deploy.env as the server.
EnvironmentFile=-$APP_DIR/deploy.env
Environment=NEXT_TELEMETRY_DISABLED=1
Environment=PATH=$(dirname "$NPM_BIN"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStart=$NPM_BIN run start -- --hostname 0.0.0.0 --port 3000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# One tunnel unit per device: fall-relay-tunnel-<user>-<host>.service
tunnel_units=()
for target in $RPI_TARGETS; do
  [[ "$target" == *@* ]] || { echo "RPI_TARGETS entries must be user@host: $target" >&2; exit 1; }
  slug="$(printf '%s' "$target" | tr -c 'A-Za-z0-9' '-')"
  unit="fall-relay-tunnel-$slug.service"
  backup "$unit"
  cat > "$UNIT_DIR/$unit" <<EOF
[Unit]
Description=Fall-Relay reverse tunnel: $target 127.0.0.1:$TUNNEL_PORT -> server :$BRIDGE_PORT
After=network-online.target $API_UNIT
Wants=network-online.target $API_UNIT

[Service]
Type=simple
ExecStart=$SSH_BIN -N -T -i $SSH_KEY -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=yes -R 127.0.0.1:$TUNNEL_PORT:127.0.0.1:$BRIDGE_PORT $target
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  tunnel_units+=("$unit")
done

systemctl --user daemon-reload
units=("$API_UNIT" "$WEB_UNIT" "${tunnel_units[@]}")
systemctl --user enable "${units[@]}"
systemctl --user restart "${units[@]}"
systemctl --user --no-pager --full status "${units[@]}"
echo "Web:       http://localhost:3000/"
echo "Cameras:   http://localhost:$BRIDGE_PORT/api/cameras"
echo "Database:  PostgreSQL, FALLRELAY_DATABASE_URL in deploy.env"
echo "Logs:      journalctl --user -u $API_UNIT -f"
