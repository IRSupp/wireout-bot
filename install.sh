#!/usr/bin/env bash
set -euo pipefail
umask 077
RAW_BASE="https://raw.githubusercontent.com/IRSupp/wireout-bot/main"
DEST=/opt/wireout
[[ "$EUID" -eq 0 ]] || { echo 'Run with sudo or as root.'; exit 1; }
command -v curl >/dev/null || { echo 'Install curl first.'; exit 1; }
if [[ -e "$DEST/app.env" || -e "$DEST/install.env" || -e "$DEST/wireout/app.env" ]]; then
  echo 'Existing installation found. Configuration was preserved. Use the upgrade guide.'
  exit 1
fi
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT
for file in setup.sh installer.py manage.sh compose.yaml; do
  curl --fail --silent --show-error --location "$RAW_BASE/$file" -o "$WORK/$file"
done
bash -n "$WORK/setup.sh" "$WORK/manage.sh"
install -d -m 700 "$DEST"
for file in setup.sh installer.py manage.sh compose.yaml; do
  install -m 600 "$WORK/$file" "$DEST/$file"
done
cd "$DEST"
bash setup.sh </dev/tty
