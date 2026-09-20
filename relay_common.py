"""Shared helpers for the muse-relay bus scripts.

Configuration (environment variables):
  MUSE_RELAY_URL   Base URL of the Upstash Redis REST endpoint, e.g.
                   https://<instance>.upstash.io  (required)
  MUSE_RELAY_BUS   Redis list name used as the bus (default: muse-bus)
  MUSE_RELAY_NICK  This participant's nick (default: milo)
  MUSE_RELAY_TOKEN_FILE  Path to the file holding the REST token
                   (default: ~/.config/muse-relay/token)

The token file must contain only the token, nothing else, and should be
chmod 600. The token is never printed by any script.
"""
import json
import os
import re
import subprocess
import time
import urllib.parse

RELAY_URL = os.environ.get("MUSE_RELAY_URL", "").rstrip("/")
BUS = os.environ.get("MUSE_RELAY_BUS", "muse-bus")
NICK = os.environ.get("MUSE_RELAY_NICK", "milo")
TOKEN_FILE = os.path.expanduser(
    os.environ.get("MUSE_RELAY_TOKEN_FILE", "~/.config/muse-relay/token"))


def clean_room(room):
    """Sanitize a room name to [a-z0-9_-]; '' means the default room."""
    clean = re.sub(r"[^a-z0-9_-]", "", (room or "").strip().lower())
    if room and not clean:
        raise ValueError(f"invalid room name: {room!r}")
    return clean


def bus_key(room=None):
    """Redis key for a room. The default room is the legacy 'muse-bus' list,
    so existing send/poll setups keep working unchanged."""
    clean = clean_room(room)
    return f"muse-bus:room:{clean}" if clean else BUS


def seen_path(room=None):
    """Read-offset file for a room, next to this module."""
    here = os.path.dirname(os.path.abspath(__file__))
    clean = clean_room(room)
    name = "seen.txt" if not clean else f"seen-{clean}.txt"
    return os.path.join(here, name)


PRESENCE_TTL = 120  # seconds a nick stays "online" after its last heartbeat
NICKS_KEY = "muse-bus:nicks"

# Max messages kept per bus list. Override with MUSE_RELAY_KEEP.
KEEP_MESSAGES = int(os.environ.get("MUSE_RELAY_KEEP", "500"))


def _presence_key(nick):
    return "muse-bus:presence:" + urllib.parse.quote(nick, safe="")


def presence_beat():
    """Heartbeat: mark this nick online for PRESENCE_TTL seconds.

    Called automatically by send.py, poll.py and watch.py on successful
    bus contact. Best-effort — callers should not fail if this does.
    """
    q = urllib.parse.quote(NICK, safe="")
    api_get(f"setex/{_presence_key(NICK)}/{PRESENCE_TTL}/1")
    api_get(f"sadd/{NICKS_KEY}/{q}")


def presence_list():
    """Return nicks with a live heartbeat key, sorted."""
    data = json.loads(api_get(f"smembers/{NICKS_KEY}"))
    nicks = sorted(n for n in (data.get("result") or [])
                   if isinstance(n, str))
    if not nicks:
        return []
    keys = "/".join(_presence_key(n) for n in nicks)
    data = json.loads(api_get(f"mget/{keys}"))
    vals = data.get("result") or []
    return [n for n, v in zip(nicks, vals) if v is not None]


def _token():
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    if not token:
        raise RuntimeError("token file is empty: " + TOKEN_FILE)
    return token


def _check_config():
    if not RELAY_URL:
        raise RuntimeError(
            "MUSE_RELAY_URL is not set; see README for setup")


def api_get(path, retries=4):
    """GET <RELAY_URL>/<path>; returns the raw response body.

    Uses curl (subprocess): urllib intermittently drops connections to
    Upstash in some environments while curl stays reliable.
    """
    _check_config()
    token = _token()
    last_err = "no attempts"
    for _ in range(retries):
        try:
            p = subprocess.run(
                ["curl", "-s", "-m", "20",
                 "-H", f"Authorization: Bearer {token}",
                 f"{RELAY_URL}/{path}"],
                capture_output=True, text=True, timeout=30)
        except Exception as e:
            last_err = f"subprocess: {e}"
            time.sleep(2)
            continue
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout
        last_err = (p.stderr.strip() or f"curl rc={p.returncode}")[:160]
        time.sleep(2)
    raise RuntimeError(f"request failed after {retries} attempts: {last_err}")


def bus_get(start, stop, key=None):
    """Return list items key[start..stop] as Python objects."""
    data = json.loads(api_get(f"lrange/{key or BUS}/{start}/{stop}"))
    return data.get("result", []) or []


def bus_trim(key=None, keep=None):
    """Trim a bus list to the newest `keep` messages (default KEEP_MESSAGES).

    Keeps the free Redis tier from filling up. Best-effort — callers should
    not fail if this does.
    """
    keep = KEEP_MESSAGES if keep is None else int(keep)
    if keep < 1:
        raise ValueError("keep must be >= 1")
    api_get(f"ltrim/{key or BUS}/-{keep}/-1")


def bus_push(body, key=None):
    """Append a plain-text string to the bus; returns the raw response."""
    _check_config()
    token = _token()
    key = key or BUS
    cmd = ["curl", "-s", "-m", "20",
           "-H", f"Authorization: Bearer {token}",
           "-H", "Content-Type: text/plain",
           "-H", "Connection: close",
           "--data-binary", "@-",
           f"{RELAY_URL}/rpush/{key}"]
    last_err = "no attempts"
    for _ in range(4):
        try:
            p = subprocess.run(cmd, input=body, capture_output=True,
                               text=True, timeout=30)
        except Exception as e:
            last_err = f"subprocess: {e}"
            time.sleep(2)
            continue
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout
        last_err = (p.stderr.strip() or f"curl rc={p.returncode}")[:160]
        time.sleep(2)
    raise RuntimeError(f"push failed after 4 attempts: {last_err}")
