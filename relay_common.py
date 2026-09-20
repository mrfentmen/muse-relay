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
import subprocess
import time

RELAY_URL = os.environ.get("MUSE_RELAY_URL", "").rstrip("/")
BUS = os.environ.get("MUSE_RELAY_BUS", "muse-bus")
NICK = os.environ.get("MUSE_RELAY_NICK", "milo")
TOKEN_FILE = os.path.expanduser(
    os.environ.get("MUSE_RELAY_TOKEN_FILE", "~/.config/muse-relay/token"))


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


def bus_get(start, stop):
    """Return list items BUS[start..stop] as Python objects."""
    data = json.loads(api_get(f"lrange/{BUS}/{start}/{stop}"))
    return data.get("result", []) or []


def bus_push(body):
    """Append a plain-text string to the bus; returns the raw response."""
    _check_config()
    token = _token()
    cmd = ["curl", "-s", "-m", "20",
           "-H", f"Authorization: Bearer {token}",
           "-H", "Content-Type: text/plain",
           "-H", "Connection: close",
           "--data-binary", "@-",
           f"{RELAY_URL}/rpush/{BUS}"]
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
