#!/usr/bin/env python3
"""English-only customer setup. Contains no bot implementation or vendor secrets."""
import base64
import getpass
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(*args, **kwargs):
    return subprocess.run(args, check=True, cwd=ROOT, **kwargs)


def ask(label, *, secret=False, default="", pattern=None):
    while True:
        value = (getpass.getpass if secret else input)(label + (f" [{default}]" if default else "") + ": ").strip() or default
        if value and not any(c in value for c in "\r\n\0") and (not pattern or re.fullmatch(pattern, value)):
            return value
        print("Please enter a valid value.")


def compose(*args):
    run("docker", "compose", "--env-file", "install.env", *args)


def verify_token(token):
    try:
        with urllib.request.urlopen('https://api.telegram.org/bot' + token + '/getMe', timeout=20) as response:
            result = json.load(response)
        if result.get('ok') is not True:
            raise ValueError('invalid token')
        print('Telegram bot verified: @' + result['result']['username'])
    except Exception:
        raise SystemExit('Telegram token could not be verified. Check the token and outbound connectivity, then retry.') from None


def main():
    os.umask(0o077)
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise SystemExit("This release requires Linux x86_64 (AMD64).")
    if os.geteuid() != 0:
        raise SystemExit("Run: sudo bash install.sh")
    print("WireOut Bot 1.1.0 - Installation")
    if (ROOT / "app.env").exists() or (ROOT / "install.env").exists():
        raise SystemExit("Existing configuration found. Nothing was overwritten. Use the upgrade guide.")
    if not shutil.which("docker"):
        raise SystemExit("Install Docker Engine and Docker Compose v2.30+ using https://docs.docker.com/engine/install/ then run again.")
    run("docker", "info", stdout=subprocess.DEVNULL)
    run("docker", "compose", "version")
    image = ask("Docker image", default="boroumandhosein/wireout-bot:1.1.0", pattern=r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]+")
    archive = ROOT / "wireout-bot-1.1.0-amd64.tar.gz"
    if archive.exists():
        checks = ROOT / "SHA256SUMS"
        expected = next((parts[0] for line in checks.read_text().splitlines() if len(parts := line.split()) == 2 and parts[1] == archive.name), None) if checks.is_file() else None
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if not expected or actual != expected:
            raise SystemExit("Image checksum mismatch. Download a fresh copy from the vendor.")
        run("docker", "load", "-i", str(archive))
    if subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run("docker", "pull", image)
    token = ask("Telegram bot token (from BotFather)", secret=True, pattern=r"[0-9]+:[A-Za-z0-9_-]+")
    verify_token(token)
    admins = ask("Owner Telegram numeric ID (comma-separated for multiple owners)", pattern=r"[1-9][0-9]*(,[1-9][0-9]*)*")
    hwid = secrets.token_hex(32)
    print("License device ID: " + hashlib.sha256(hwid.encode()).hexdigest())
    print("Provide this device ID to the vendor if device activation is required.")
    license_key = ask("License key", secret=True)
    username = ask("Web admin username", default="admin", pattern=r"[a-zA-Z0-9_.-]+")
    while True:
        password = ask("Web admin password (at least 12 characters)", secret=True)
        if len(password) >= 12 and password == getpass.getpass("Confirm web admin password: "):
            break
        print("Passwords must match and contain at least 12 characters.")
    port = ask("Web panel port", default="8080", pattern=r"[0-9]{1,5}")
    if not 1024 <= int(port) <= 65535:
        raise SystemExit("Port must be between 1024 and 65535.")
    db_password = secrets.token_hex(32)
    config = {
        "BOT_TOKEN": token, "ADMIN_IDS": admins, "LICENSE_KEY": license_key,
        "HARDWARE_ID": hwid, "FERNET_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "WEB_USERNAME": username, "WEB_PASSWORD": password, "WEB_SECRET": secrets.token_hex(32),
        "DATABASE_URL": f"postgresql+asyncpg://wireout:{db_password}@db:5432/wireout",
        "WEB_HOST": "0.0.0.0", "WEB_PORT": "8080", "TIMEZONE": "Asia/Tehran",
    }
    (ROOT / "app.env").write_text("".join(f"{key}={value}\n" for key, value in config.items()))
    (ROOT / "install.env").write_text(f"WIREOUT_IMAGE={image}\nDB_PASSWORD={db_password}\nWEB_PORT={port}\n")
    compose("config", "--quiet")
    print("Checking the license before starting the bot...")
    result = subprocess.run(["docker", "run", "--rm", "--env-file", str(ROOT / "app.env"), image, "check-license"], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode:
        raise SystemExit("License check failed. Configuration is saved. Contact the vendor, update LICENSE_KEY in app.env if needed, then follow the activation guide.")
    print("License verified successfully.")
    compose("up", "-d", "--wait", "db")
    compose("run", "--rm", "init")
    compose("up", "-d", "bot", "web")
    print(f"Installation complete. Web panel: http://YOUR_SERVER_IP:{port}")
    print(f"Replace YOUR_SERVER_IP with your server's public IP. Allow TCP port {port} in your server/provider firewall if required.")
    print("For encrypted browser access, configure a domain and HTTPS reverse proxy.")
    print("Keep app.env and database backups private. Never delete or regenerate FERNET_KEY or HARDWARE_ID during upgrades.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, subprocess.CalledProcessError):
        raise SystemExit("Installation interrupted or a command failed. Existing data was not deleted.")
