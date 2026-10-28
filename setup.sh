#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ "$EUID" -ne 0 ]]; then
  echo "Run: sudo bash install.sh"
  exit 1
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo "This release requires Linux x86_64 (AMD64)."
  exit 1
fi
if ! command -v python3 >/dev/null || ! command -v docker >/dev/null; then
  . /etc/os-release
  if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]]; then
    echo "Automatic prerequisite installation supports Ubuntu 24.04 only. Install Python 3.11+ and Docker manually on other distributions."
    exit 1
  fi
  echo "Installing missing prerequisites automatically from Ubuntu and Docker's official repositories..."
  apt-get update
  apt-get install -y python3 ca-certificates curl
  if ! command -v docker >/dev/null; then
    for package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
      if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
        echo "Conflicting package found: $package. Resolve it using https://docs.docker.com/engine/install/ubuntu/ and run again."
        exit 1
      fi
    done
    if [[ -e /etc/apt/sources.list.d/docker.sources || -e /etc/apt/sources.list.d/docker.list ]]; then
      echo "An existing Docker repository configuration was found. Complete Docker installation using that configuration, then run again."
      exit 1
    fi
    install -m 0755 -d /etc/apt/keyrings
    curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    cat > /etc/apt/sources.list.d/docker.sources <<'DOCKER_REPO'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER_REPO
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
  fi
fi
if ! docker compose version >/dev/null 2>&1; then
  echo 'Docker Compose v2 is missing. Install the Docker Compose plugin for your existing Docker installation, then run again.'
  exit 1
fi
exec python3 installer.py "$@"
