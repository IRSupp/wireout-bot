"""Durable, single-use onboarding state. Standard library only."""
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import tempfile
import time

ROOT = Path(os.environ.get('WG_AGENT_STATE_DIR', '/opt/wireout-wg-agent'))


@contextlib.contextmanager
def locked():
    with (ROOT / 'pairing.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read():
    try:
        return json.loads((ROOT / 'pairing.json').read_text())
    except FileNotFoundError:
        return {}


def save(data):
    with tempfile.NamedTemporaryFile(mode='w', dir=ROOT, delete=False) as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
        tmp = f.name
    os.replace(tmp, ROOT / 'pairing.json')
    fd = os.open(ROOT, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def issue(port=8787, ttl=600):
    secret = secrets.token_hex(32)
    cert = ssl.PEM_cert_to_DER_cert((ROOT / 'agent.crt').read_text())
    fingerprint = hashlib.sha256(cert).hexdigest()
    with locked():
        state = read()
        state.update(code_hash=hashlib.sha256(secret.encode()).hexdigest(), expires=time.time()+ttl, used=False)
        save(state)
    return f'WO1.{port}.{fingerprint}.{secret}'


def claim(secret, token):
    if not isinstance(secret, str) or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token):
        return False
    with locked():
        state = read()
        if state.get('used', True) or time.time() >= state.get('expires', 0):
            return False
        if not hmac.compare_digest(state.get('code_hash', ''), hashlib.sha256(secret.encode()).hexdigest()):
            return False
        state.update(used=True, token=token)
        state.pop('code_hash', None)
        save(state)
    return True


def token(default):
    return read().get('token', default)


def metadata():
    return json.loads((ROOT / 'server.json').read_text())


if __name__ == '__main__':
    os.umask(0o077)
    print('One-time connection code (valid for 10 minutes). Paste it into WireOut Bot:')
    print(issue())
