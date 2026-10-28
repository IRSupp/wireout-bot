#!/usr/bin/env bash
set -euo pipefail
umask 077

# Set to this repository's raw GitHub URL when publishing.
RAW_BASE="https://raw.githubusercontent.com/IRSupp/wireout-bot/main"
DEST=/opt/wireout-wg-agent
UNIT=/etc/systemd/system/wireout-wg-agent.service

[[ "$EUID" -eq 0 ]] || { echo 'Run this installer with sudo or as root.'; exit 1; }
[[ "$(uname -s)" == Linux ]] || { echo 'Linux is required.'; exit 1; }
for command in python3 wg wg-quick systemctl; do
  command -v "$command" >/dev/null || { echo "Missing prerequisite: $command. Install Python 3 and WireGuard first."; exit 1; }
done
if [[ -e "$DEST" || -e "$UNIT" ]]; then
  echo "Existing installation found. Nothing was overwritten. See the upgrade section in README.md."
  exit 1
fi
if [[ ! -r /dev/tty ]]; then
  echo 'An interactive SSH terminal is required.'; exit 1
fi
read -r -p 'Existing WireGuard interface [wg0]: ' IFACE </dev/tty
IFACE=${IFACE:-wg0}
[[ "$IFACE" =~ ^[a-zA-Z0-9_=+.-]{1,15}$ ]] || { echo 'Invalid interface name.'; exit 1; }
[[ -f "/etc/wireguard/$IFACE.conf" ]] || { echo 'Persistent WireGuard configuration is missing.'; exit 1; }
wg show "$IFACE" >/dev/null
read -r -p 'Agent bind IPv4 address [127.0.0.1]: ' HOST </dev/tty
HOST=${HOST:-127.0.0.1}
python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$HOST"
read -r -p 'Agent TCP port [8787]: ' PORT </dev/tty
PORT=${PORT:-8787}
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] && (( 10#$PORT >= 1024 && 10#$PORT <= 65535 )) || { echo 'Port must be between 1024 and 65535.'; exit 1; }
PORT=$((10#$PORT))
python3 - "$HOST" "$PORT" <<'PY'
import socket, sys
with socket.socket() as connection:
    connection.bind((sys.argv[1], int(sys.argv[2])))
PY
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/dev/stdin}")" && pwd)
if [[ -f "$SOURCE_DIR/agent.py" ]]; then
  cp -- "$SOURCE_DIR/agent.py" "$WORK/agent.py"
else
  command -v curl >/dev/null || { echo 'curl is required to download the agent.'; exit 1; }
  curl --fail --silent --show-error --location "$RAW_BASE/agent/agent.py" -o "$WORK/agent.py"
fi
python3 - "$WORK/agent.py" <<'PY'
import ast, pathlib, sys
ast.parse(pathlib.Path(sys.argv[1]).read_text())
PY
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
install -d -m 700 "$DEST"
install -m 600 "$WORK/agent.py" "$DEST/agent.py"
printf 'WG_AGENT_TOKEN=%s\nWG_AGENT_IFACE=%s\nWG_AGENT_HOST=%s\nWG_AGENT_PORT=%s\n' "$TOKEN" "$IFACE" "$HOST" "$PORT" > "$DEST/agent.env"
cat > "$UNIT" <<'UNIT'
[Unit]
Description=WireOut WireGuard management agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/wireout-wg-agent
EnvironmentFile=/opt/wireout-wg-agent/agent.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 /opt/wireout-wg-agent/agent.py
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable --now wireout-wg-agent
if ! python3 - "$DEST/agent.env" <<'PY'
import json, pathlib, sys, time, urllib.request
env = dict(line.split('=', 1) for line in pathlib.Path(sys.argv[1]).read_text().splitlines())
host = env['WG_AGENT_HOST']
if host == '0.0.0.0': host = '127.0.0.1'
request = urllib.request.Request('http://' + host + ':' + env['WG_AGENT_PORT'] + '/health',
    headers={'Authorization': 'Bearer ' + env['WG_AGENT_TOKEN']})
for _ in range(20):
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            data = json.load(response)
        if data.get('ok') is True: break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit('Agent health check failed. Inspect: journalctl -u wireout-wg-agent -n 50')
PY
then
  systemctl stop wireout-wg-agent
  echo 'Agent stopped after failed health check. Configuration is preserved for troubleshooting.'
  exit 1
fi
echo 'Agent installed and health check passed.'
echo "Bind address: http://$HOST:$PORT"
echo 'Read the token privately: sudo cat /opt/wireout-wg-agent/agent.env'
echo 'Use a URL reachable from the bot. Protect remote access with HTTPS or an encrypted private network.'
