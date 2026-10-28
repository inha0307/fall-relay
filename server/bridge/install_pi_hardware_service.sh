#!/usr/bin/env bash
# One agent per physical Pi, independent of its per-camera inference workers.
set -euo pipefail
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${1:?Usage: install_pi_hardware_service.sh INSTALL_ROOT CONFIG_PATH}"
CONFIG_SRC="${2:?Provide a device-local hardware config}"
PYTHON="${PI_PYTHON:-/usr/bin/python3}"
case "$ROOT$CONFIG_SRC$PYTHON" in *[[:space:]]*) echo 'Paths with spaces are not supported.' >&2; exit 1 ;; esac
mkdir -p "$ROOT" "$HOME/.config/systemd/user"
STAMP="$(date +%Y%m%d%H%M%S)"
for name in pi_hardware_agent.py pi_hardware_config.json; do
  if [[ -f "$ROOT/$name" ]]; then cp -a "$ROOT/$name" "$ROOT/$name.backup.$STAMP"; fi
done
if [[ "$(realpath "$SOURCE_DIR/pi_hardware_agent.py")" != "$(realpath -m "$ROOT/pi_hardware_agent.py")" ]]; then
  install -m 0644 "$SOURCE_DIR/pi_hardware_agent.py" "$ROOT/pi_hardware_agent.py"
fi
if [[ "$(realpath "$CONFIG_SRC")" != "$(realpath -m "$ROOT/pi_hardware_config.json")" ]]; then
  install -m 0600 "$CONFIG_SRC" "$ROOT/pi_hardware_config.json"
fi
CAMERA_ID="$("$PYTHON" - "$ROOT" <<'PY'
import sys,re
sys.path.insert(0,sys.argv[1])
from pi_hardware_agent import load_config
cfg=load_config(sys.argv[1]+'/pi_hardware_config.json')
if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',cfg['camera_id']):raise SystemExit('Invalid camera ID')
print(cfg['camera_id'])
PY
)"
NAME=fall-relay-device-agent.service
UNIT="$HOME/.config/systemd/user/$NAME"
if [[ -f "$UNIT" ]]; then cp -a "$UNIT" "$UNIT.backup.$STAMP"; fi
cat > "$UNIT" <<EOF
[Unit]
Description=Fall-Relay device agent: telemetry and remote actions (one per device)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$ROOT
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-$ROOT/camera_token_cam$CAMERA_ID.env
ExecStart=$PYTHON $ROOT/pi_hardware_agent.py --config $ROOT/pi_hardware_config.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now "$NAME"
systemctl --user restart "$NAME"
systemctl --user --no-pager status "$NAME"
