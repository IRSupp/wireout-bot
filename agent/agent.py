#!/usr/bin/env python3
# deploy/wg_agent/agent.py
#
# ── IRSupp WireGuard Agent ──
# یک سرویس HTTP سبک که روی هر سرور WireGuard اجرا می‌شود و به ربات اجازه می‌دهد
# peer اضافه/حذف کند و آمار مصرف همه‌ی peerها را یکجا بگیرد — بدون SSH.
#
# چرا این agent؟ در مدل SSH، ربات برای هر عملیات یک اتصال باز می‌کند؛ با زیادشدن
# سرورها/کاربران این پرهزینه می‌شود. این agent یک نقطه‌ی تماس تمیز می‌دهد و
# `/transfer` آمار *همه‌ی* peerها را در یک درخواست برمی‌گرداند (پایش مقیاس‌پذیر).
#
# طراحی:
#   • فقط کتابخانه‌ی استاندارد پایتون (بدون pip/venv) → نصب و نگه‌داری ساده.
#   • احراز هویت با توکن Bearer (مقایسه‌ی زمان‌ثابت).
#   • اجرای دستورها با subprocess به‌صورت لیست آرگومان (بدون shell) → ضد تزریق.
#   • ربات همچنان کلید و IP را خودش تولید می‌کند؛ agent فقط مجری ساده و بی‌حالت است.
#
# پیکربندی (از متغیر محیطی، توسط systemd ست می‌شود):
#   WG_AGENT_TOKEN   : توکن احراز هویت (اجباری)
#   WG_AGENT_IFACE   : نام اینترفیس (پیش‌فرض wg0)
#   WG_AGENT_HOST    : آدرس bind (پیش‌فرض 0.0.0.0)
#   WG_AGENT_PORT    : پورت (پیش‌فرض 8787)
#   WG_BIN / WGQUICK_BIN : مسیر اختیاری باینری‌ها
#
# API:
#   GET  /health              → {"ok":true,"iface":"wg0","peers":N}
#   GET  /transfer            → {"transfers":{"<pubkey>": total_bytes, ...}}
#   POST /peer/add   {"public_key","allowed_ip"}  → {"ok":true}
#   POST /peer/remove{"public_key"}               → {"ok":true}
#
# نکته‌ی امنیتی: این سرویس با توکن محافظت می‌شود، ولی چون HTTP ساده است توصیه
# می‌شود پورت آن با فایروال فقط به IP سرور ربات باز باشد (در اسکریپت نصب توضیح داده شده).

import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [wg-agent] %(levelname)s %(message)s")
log = logging.getLogger("wg-agent")

TOKEN = os.environ.get("WG_AGENT_TOKEN", "")
IFACE = os.environ.get("WG_AGENT_IFACE", "wg0")
HOST = os.environ.get("WG_AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("WG_AGENT_PORT", "8787"))
WG_BIN = os.environ.get("WG_BIN") or shutil.which("wg") or "/usr/bin/wg"
WGQUICK_BIN = os.environ.get("WGQUICK_BIN") or shutil.which("wg-quick") or "/usr/bin/wg-quick"

# اعتبارسنجی ورودی‌ها (ضد تزریق و داده‌ی خراب)
_PUBKEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")          # کلید عمومی WireGuard (base64 44)
_IP_RE = re.compile(r"^[0-9a-fA-F:.]+(/\d{1,3})?$")        # IPv4/IPv6 با CIDR اختیاری
_MUTATION_LOCK = threading.Lock()
MAX_BODY = 4096


def _run(args) -> tuple[int, str, str]:
    """اجرای یک دستور به‌صورت لیست آرگومان (بدون shell)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _valid_pubkey(pk: str) -> bool:
    return isinstance(pk, str) and bool(_PUBKEY_RE.fullmatch(pk))


def _valid_ip(ip: str) -> bool:
    if not isinstance(ip, str) or not _IP_RE.fullmatch(ip):
        return False
    try:
        addr = ipaddress.ip_interface(ip)
        return addr.network.prefixlen == addr.max_prefixlen
    except ValueError:
        return False


def op_health() -> dict:
    rc, out, err = _run([WG_BIN, "show", IFACE])
    if rc != 0:
        raise RuntimeError(err or out or "wg show failed")
    peers = out.count("peer:")
    live = {line.split(':', 1)[1].strip() for line in out.splitlines() if line.strip().startswith('peer:')}
    try:
        saved = {line.split('=', 1)[1].strip() for line in
                 Path('/etc/wireguard/' + IFACE + '.conf').read_text().splitlines()
                 if line.strip().startswith('PublicKey') and '=' in line}
        persistent = live == saved
    except OSError:
        persistent = False
    return {"ok": True, "iface": IFACE, "peers": peers, "persistent": persistent}


def op_transfer() -> dict:
    """آمار مصرف همه‌ی peerها را یکجا برمی‌گرداند: {pubkey: rx+tx}."""
    rc, out, err = _run([WG_BIN, "show", IFACE, "transfer"])
    if rc != 0:
        raise RuntimeError(err or out or "wg transfer failed")
    transfers = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            try:
                transfers[parts[0]] = int(parts[1]) + int(parts[2])  # rx + tx
            except ValueError:
                continue
    return {"transfers": transfers}


def _op_peer_add(pk: str, ip: str) -> dict:
    if not _valid_pubkey(pk):
        raise ValueError("public_key نامعتبر")
    if not _valid_ip(ip):
        raise ValueError("allowed_ip نامعتبر")
    _persist_mutation([WG_BIN, "set", IFACE, "peer", pk, "allowed-ips", ip])
    return {"ok": True}


def _op_peer_remove(pk: str) -> dict:
    if not _valid_pubkey(pk):
        raise ValueError("public_key نامعتبر")
    # `wg set ... remove` برای peerِ ناموجود هم کد ۰ می‌دهد (ایده‌مپوتنت)؛ پس کد غیرصفر
    # یعنی شکست واقعی (اینترفیس/مجوز) و باید گزارش شود تا ربات دوباره تلاش کند.
    _persist_mutation([WG_BIN, "set", IFACE, "peer", pk, "remove"])
    return {"ok": True}


def _persist_mutation(command):
    rc, previous, _ = _run([WG_BIN, 'showconf', IFACE])
    if rc:
        raise RuntimeError('Cannot snapshot WireGuard configuration')
    rc, _, _ = _run(command)
    if rc:
        raise RuntimeError('WireGuard mutation failed')
    rc, _, _ = _run([WGQUICK_BIN, 'save', IFACE])
    if not rc:
        return
    # Never acknowledge a non-persistent mutation. Restore just the configuration,
    # using syncconf so unaffected peers retain their sessions and counters.
    with tempfile.NamedTemporaryFile(mode='w', prefix='wg-agent-', suffix='.conf') as snapshot:
        snapshot.write(previous)
        snapshot.flush()
        restored, _, _ = _run([WG_BIN, 'syncconf', IFACE, snapshot.name])
    if restored:
        log.error('WireGuard persistence AND rollback failed; operator attention required')
    raise RuntimeError('WireGuard save failed' + ('; rollback failed' if restored else '; change rolled back'))


def op_peer_add(pk: str, ip: str) -> dict:
    # wg-quick save uses a shared temporary file; concurrent writers can corrupt it.
    with _MUTATION_LOCK:
        return _op_peer_add(pk, ip)


def op_peer_remove(pk: str) -> dict:
    with _MUTATION_LOCK:
        return _op_peer_remove(pk)


class Handler(BaseHTTPRequestHandler):
    server_version = "wg-agent/1.0"

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix):
            return False
        given = auth[len(prefix):]
        return bool(TOKEN) and hmac.compare_digest(given.encode("utf-8"), TOKEN.encode("utf-8"))

    def _body_json(self) -> dict:
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer-Encoding unsupported")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("Request body must be 1..4096 bytes")
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            return data
        except (ValueError, UnicodeError, OSError) as exc:
            raise ValueError("Invalid request body") from exc

    # لاگ ساکت‌تر
    def log_message(self, fmt, *args):  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):  # noqa: N802
        if not self._authed():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            if self.path == "/health":
                self._send(200, op_health())
            elif self.path == "/transfer":
                self._send(200, op_transfer())
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": str(e)})

    def do_POST(self):  # noqa: N802
        if not self._authed():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            data = self._body_json()
            if self.path == "/peer/add":
                self._send(200, op_peer_add(data.get("public_key", ""),
                                            data.get("allowed_ip", "")))
            elif self.path == "/peer/remove":
                self._send(200, op_peer_remove(data.get("public_key", "")))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except ValueError as e:
            self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": str(e)})


def main():
    if not TOKEN:
        log.error("WG_AGENT_TOKEN تنظیم نشده — سرویس بالا نمی‌آید.")
        sys.exit(1)
    log.info("agent روی %s:%s برای اینترفیس %s (wg=%s)", HOST, PORT, IFACE, WG_BIN)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
