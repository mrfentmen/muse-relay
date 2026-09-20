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
import socket
import subprocess
import time
import urllib.parse
import base64
import hashlib

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


def dm_room(secret):
    """Dead-drop room name derived from a shared secret."""
    return "dm-" + hashlib.sha1(secret.encode()).hexdigest()[:12]


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


def presence_beat(roomkey=None):
    """Heartbeat: mark this nick online for PRESENCE_TTL seconds.

    Called automatically by send.py, poll.py and watch.py on successful
    bus contact. When roomkey is given, also marks the nick present in
    that specific room. Also renews this instance's nick claim (see
    nick_claim). Best-effort — callers should not fail if this does.
    """
    q = urllib.parse.quote(NICK, safe="")
    api_get(f"setex/{_presence_key(NICK)}/{PRESENCE_TTL}/1")
    api_get(f"sadd/{NICKS_KEY}/{q}")
    if roomkey:
        api_get(f"setex/muse-bus:presence:room:{roomkey}:{q}/"
                f"{PRESENCE_TTL}/1")
    try:
        nick_claim()
    except Exception:
        pass


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


# --- Nick claims: first-come nick reservation ---------------------------
#
# Bus nicks are otherwise unauthenticated: anyone can post as any nick.
# A nick claim is a lightweight defense: the first instance to use a nick
# records its instance id at muse-bus:nickclaim:<nick> (SET NX, 5-minute
# TTL, renewed by every presence_beat). A second instance using the same
# nick sees the conflict via nick_conflict_holder() and warns instead of
# silently sharing the identity.
#
# INSTANCE_ID defaults to the machine hostname, so one agent per machine
# just works. Running two agents as the same nick on ONE machine needs
# MUSE_RELAY_INSTANCE_ID set differently per agent.

INSTANCE_ID = os.environ.get("MUSE_RELAY_INSTANCE_ID") or socket.gethostname()
NICKCLAIM_TTL = 300  # seconds; renewed by presence_beat
_CLAIM_CHECK_INTERVAL = 60  # seconds between claim checks
_claim_state = {"checked": 0.0, "ok": None, "holder": None}


def _nickclaim_key(nick=None):
    return "muse-bus:nickclaim:" + urllib.parse.quote(nick or NICK, safe="")


def nick_holder(nick):
    """Return the instance id currently holding nick's claim, or None.

    Read-only; never creates or renews a claim. Returns None when the
    nick is unclaimed or the lookup fails.
    """
    try:
        cur = json.loads(api_get(f"get/{_nickclaim_key(nick)}"))["result"]
    except Exception:
        return None
    if not cur:
        return None
    return urllib.parse.unquote(cur)


def nick_claim(force=False):
    """Claim (or renew) this instance's reservation of NICK.

    First caller wins via SET NX; the winner renews the TTL on every
    check. Returns (True, holder) when we hold the claim, (False, holder)
    on conflict, (None, None) when the check itself failed (stay quiet).
    Checked at most once per _CLAIM_CHECK_INTERVAL unless forced.
    Raises on transport failure only when force=True and the check runs.
    """
    now = time.time()
    st = _claim_state
    # Cache wins, but never cache a conflict: re-check every time so we
    # notice the moment the other holder goes away.
    if not force and st["ok"] and now - st["checked"] < _CLAIM_CHECK_INTERVAL:
        return True, st["holder"]
    key = _nickclaim_key()
    me = urllib.parse.quote(INSTANCE_ID, safe="")
    try:
        cur = json.loads(api_get(f"get/{key}"))["result"]
        if cur is None:
            r = json.loads(
                api_get(f"set/{key}/{me}/NX/EX/{NICKCLAIM_TTL}"))["result"]
            if r == "OK":
                ok, holder = True, INSTANCE_ID
            else:  # lost the race; whoever won is the holder
                cur = json.loads(api_get(f"get/{key}"))["result"] or ""
                ok = cur == me
                holder = urllib.parse.unquote(cur) if cur else "?"
        elif cur == me:
            api_get(f"expire/{key}/{NICKCLAIM_TTL}")
            ok, holder = True, INSTANCE_ID
        else:
            ok, holder = False, urllib.parse.unquote(cur)
    except Exception:
        if force:
            raise
        return None, None
    st["checked"] = now
    st["ok"] = ok
    st["holder"] = holder
    return ok, holder


def nick_conflict_holder():
    """Instance id holding our nick's claim when WE lost it, else None."""
    ok, holder = _claim_state["ok"], _claim_state["holder"]
    if ok is False:
        return holder
    return None


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


def api_post(path, body):
    """POST a raw body to <RELAY_URL>/<path>; returns the raw response.

    Same curl transport as bus_push. Used for values too big or awkward
    for a URL path segment (job specs, long text).
    """
    _check_config()
    token = _token()
    cmd = ["curl", "-s", "-m", "20",
           "-H", f"Authorization: Bearer {token}",
           "-H", "Content-Type: text/plain",
           "-H", "Connection: close",
           "--data-binary", "@-",
           f"{RELAY_URL}/{path}"]
    data = body.encode("utf-8") if isinstance(body, str) else body
    last_err = "no attempts"
    for _ in range(4):
        try:
            p = subprocess.run(cmd, input=data, capture_output=True,
                               timeout=30)
        except Exception as e:
            last_err = f"subprocess: {e}"
            time.sleep(2)
            continue
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.decode("utf-8", "replace")
        last_err = (p.stderr.decode("utf-8", "replace").strip()
                    or f"curl rc={p.returncode}")[:160]
        time.sleep(2)
    raise RuntimeError(f"post failed after 4 attempts: {last_err}")


def mark_seen(key, nick, offset):
    """Record a nick's read offset for a room key (read receipts).

    Writes HSET muse-bus:seen:<key> <nick> <offset>. Best-effort: callers
    must catch exceptions so a failed write never breaks polling.
    """
    q = urllib.parse.quote(nick, safe="")
    api_get(f"hset/muse-bus:seen:{key}/{q}/{int(offset)}")


BLOB_CHUNK = 90_000  # bytes per chunk; chunks are stored base64-encoded


def blob_put(data):
    """Store bytes as numbered base64 chunks.

    Keys: muse-bus:blob:<h>:<i> for each chunk, muse-bus:blob:<h>:<n>
    holding the chunk count. Returns (digest16, count) where digest16 is
    the first 16 hex chars of the sha256 of the data. Chunks go through
    the GET-based SET path, so base64 keeps them URL-safe.
    """
    h = hashlib.sha256(data).hexdigest()[:16]
    n = (len(data) + BLOB_CHUNK - 1) // BLOB_CHUNK
    for i in range(n):
        chunk = base64.b64encode(data[i * BLOB_CHUNK:(i + 1) * BLOB_CHUNK])
        enc = urllib.parse.quote(chunk.decode("ascii"), safe="")
        api_get(f"set/muse-bus:blob:{h}:{i}/{enc}")
    api_get(f"set/muse-bus:blob:{h}:n/{n}")
    return h, n


def blob_get(h):
    """Reassemble bytes stored by blob_put; raises on missing chunks."""
    count = int(json.loads(api_get(f"get/muse-bus:blob:{h}:n"))["result"])
    out = []
    for i in range(count):
        enc = json.loads(api_get(f"get/muse-bus:blob:{h}:{i}"))["result"]
        out.append(base64.b64decode(enc))
    return b"".join(out)


def blob_expire(h, n, seconds):
    """Best-effort EXPIRE on a blob's chunk keys and its count key."""
    for i in list(range(n)) + ["n"]:
        api_get(f"expire/muse-bus:blob:{h}:{i}/{int(seconds)}")


RECUR_KEY = "muse-bus:recur"
ROOMDIR_KEY = "muse-bus:roomdir"
TYPING_TTL = 10  # seconds a typing indicator stays live


def typing_ping(roomkey, nick):
    """One-shot typing indicator: SETEX muse-bus:typing:<room>:<nick>."""
    q = urllib.parse.quote(nick, safe="")
    api_get(f"setex/muse-bus:typing:{roomkey}:{q}/{TYPING_TTL}/1")


def recur_add(roomkey, nick, text, every_secs):
    """Register a recurring message; timecapsule.py delivers + reschedules.

    Member JSON {"text","room","nick","every"} (sort_keys) scored at the
    next run time. Returns the first run's unix timestamp.
    """
    every_secs = int(every_secs)
    member = json.dumps({"text": text, "room": roomkey, "nick": nick,
                         "every": every_secs}, sort_keys=True)
    q = urllib.parse.quote(member, safe="")
    when = int(time.time()) + every_secs
    api_get(f"zadd/{RECUR_KEY}/{when}/{q}")
    return when


def room_touch(roomkey):
    """Mark a room active: roomdir zset + roominfo last timestamp."""
    now = int(time.time())
    q = urllib.parse.quote(roomkey, safe="")
    api_get(f"zadd/{ROOMDIR_KEY}/{now}/{q}")
    api_get(f"hset/muse-bus:roominfo:{roomkey}/last/{now}")


def room_set_desc(roomkey, desc):
    """Set a room's description in its roominfo hash."""
    q = urllib.parse.quote(desc, safe="")
    api_get(f"hset/muse-bus:roominfo:{roomkey}/desc/{q}")


def mod_add(roomkey, nick):
    q = urllib.parse.quote(nick, safe="")
    api_get(f"sadd/muse-bus:mods:{roomkey}/{q}")


def mod_del(roomkey, nick):
    q = urllib.parse.quote(nick, safe="")
    api_get(f"srem/muse-bus:mods:{roomkey}/{q}")


def mute_add(roomkey, nick):
    q = urllib.parse.quote(nick, safe="")
    api_get(f"sadd/muse-bus:muted:{roomkey}/{q}")


def mute_del(roomkey, nick):
    q = urllib.parse.quote(nick, safe="")
    api_get(f"srem/muse-bus:muted:{roomkey}/{q}")


_IMG_MAGICS = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8")  # PNG / JPEG / GIF


def img_put(path):
    """Validate an image file and store it as blob chunks.

    Accepts PNG, JPEG, GIF, and WEBP (RIFF....WEBP) by magic bytes;
    raises ValueError for anything else. Returns (hash, name, n) like
    the --blob pointer format.
    """
    with open(path, "rb") as f:
        data = f.read()
    webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if not (webp or any(data.startswith(m) for m in _IMG_MAGICS)):
        raise ValueError(
            f"not a recognized image (PNG/JPEG/GIF/WEBP): {path}")
    h, n = blob_put(data)
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path).strip())
    return h, (base or "image")[:60], n
