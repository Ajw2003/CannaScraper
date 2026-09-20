"""Settings, and a password on the actions that cause outbound scraping.

Reads stay open: anyone with the link can search and see stock. Writes --
a live re-check, a province rebuild -- need the password, because they spend a
shared rate-limit budget and send traffic to cannacabana.com from this
machine's IP.

Deliberately NOT trusted: the loopback address. cloudflared connects to
127.0.0.1, so every request arriving through the public tunnel looks local. A
"localhost is the owner" shortcut would hand the admin surface to the whole
internet.

Stdlib only -- hashlib, hmac, secrets. No new dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request

import paths

SETTINGS_PATH = Path(paths.data("settings.json"))
COOKIE = "cc_admin"
SESSION_HOURS = 24 * 30

DEFAULTS = {
    "port": 8000,
    "open_browser": True,
    # "quick" = a fresh trycloudflare.com URL each start.
    # "named" = your own hostname, needs tunnel_token.
    # "off"   = local and LAN only.
    "tunnel": "quick",
    "tunnel_token": "",
    "password_hash": "",
    "password_salt": "",
    "session_secret": "",
}

# Ambiguous glyphs removed: no 0/O, no 1/l/I. This gets typed on a phone.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


# --- settings --------------------------------------------------------------

def load() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            # A corrupt settings file must not stop the app booting; the
            # defaults are all usable and the file gets rewritten below.
            pass
    return s


def save(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n",
                             encoding="utf-8")


# --- password --------------------------------------------------------------

def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                          n=2 ** 14, r=8, p=1, dklen=32).hex()


def set_password(password: str) -> None:
    s = load()
    salt = secrets.token_hex(16)
    s["password_salt"] = salt
    s["password_hash"] = _hash(password, salt)
    if not s["session_secret"]:
        s["session_secret"] = secrets.token_hex(32)
    save(s)


def check_password(password: str) -> bool:
    s = load()
    if not s["password_hash"] or not s["password_salt"]:
        return False
    return hmac.compare_digest(_hash(password, s["password_salt"]),
                               s["password_hash"])


def ensure_configured() -> str | None:
    """Make sure a password and session secret exist.

    Returns the generated password the first time, so the launcher can print
    it once, and None on every later start -- we only keep the hash.
    """
    s = load()
    changed = False
    if not s["session_secret"]:
        s["session_secret"] = secrets.token_hex(32)
        changed = True

    generated = None
    if not s["password_hash"]:
        generated = "-".join(
            "".join(secrets.choice(_ALPHABET) for _ in range(4))
            for _ in range(3))
        salt = secrets.token_hex(16)
        s["password_salt"] = salt
        s["password_hash"] = _hash(generated, salt)
        changed = True

    if changed:
        save(s)
    return generated


# --- sessions --------------------------------------------------------------

def _sign(payload: str) -> str:
    s = load()
    # Bind the signature to the password hash so changing the password
    # invalidates every session that was issued under the old one.
    key = (s["session_secret"] + s["password_hash"]).encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_token(hours: int = SESSION_HOURS) -> str:
    exp = str(int(time.time() + hours * 3600))
    return f"{exp}.{_sign(exp)}"


def verify_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    exp, _, sig = token.partition(".")
    if not hmac.compare_digest(_sign(exp), sig):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


# --- login throttling ------------------------------------------------------
# Behind the tunnel every request has the same source address, so per-IP
# counting alone would be useless. Cloudflare passes the real client in
# CF-Connecting-IP; we use it when present and keep a global counter as well,
# so a distributed guessing attempt still slows down.

_fails: dict[str, list] = {}
_global_fails: list = [0, 0.0]
_LOCKOUT_AFTER = 5
_LOCKOUT_S = 30


def client_key(request: Request) -> str:
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get(
        "x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def throttled(key: str) -> float:
    """Seconds the caller must wait, or 0."""
    now = time.time()
    for count, until in (_fails.get(key, [0, 0.0]), _global_fails):
        if count >= _LOCKOUT_AFTER and until > now:
            return round(until - now, 1)
    return 0.0


def note_failure(key: str) -> None:
    now = time.time()
    for rec in (_fails.setdefault(key, [0, 0.0]), _global_fails):
        rec[0] += 1
        if rec[0] >= _LOCKOUT_AFTER:
            # Escalating, capped at 15 minutes.
            rec[1] = now + min(_LOCKOUT_S * (rec[0] - _LOCKOUT_AFTER + 1), 900)


def note_success(key: str) -> None:
    _fails.pop(key, None)
    _global_fails[0] = 0
    _global_fails[1] = 0.0


# --- the dependency --------------------------------------------------------

def require_admin(request: Request) -> bool:
    if verify_token(request.cookies.get(COOKIE, "")):
        return True
    raise HTTPException(status_code=401, detail="password required")
