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

# Max chars for a chat message body, enforced by send.py and the EDIT
# protocol (edits.py). Long content should ride --blob instead.
# Override with MUSE_RELAY_MAX_TEXT.
MAX_TEXT = int(os.environ.get("MUSE_RELAY_MAX_TEXT", "2000"))


def _presence_key(nick):
    return "muse-bus:presence:" + urllib.parse.quote(nick, safe="")


# --- Status messages -----------------------------------------------------#
# A nick can carry a free-text status ("heads down", "in a call") that
# lives at muse-bus:status:<nick> with the same 120s TTL as presence:
# every presence beat renews it, so it fades when the nick goes quiet.
# Empty text clears it.

STATUS_TTL = PRESENCE_TTL  # 120s, renewed like a heartbeat


def status_key(nick=None):
    """Redis key holding a nick's status text."""
    return "muse-bus:status:" + urllib.parse.quote(nick or NICK, safe="")


def status_set(text, nick=None):
    """Set this nick's status; empty text clears it.

    Raises ValueError when text exceeds MAX_TEXT (same limit as send).
    """
    nick = nick or NICK
    if text:
        if len(text) > MAX_TEXT:
            raise ValueError(
                f"status too long ({len(text)} chars; max {MAX_TEXT})")
        q = urllib.parse.quote(text, safe="")
        api_get(f"setex/{status_key(nick)}/{STATUS_TTL}/{q}")
    else:
        api_get(f"del/{status_key(nick)}")


def status_get(nick):
    """Return nick's live status text, or None when unset/expired."""
    try:
        cur = json.loads(api_get(f"get/{status_key(nick)}"))["result"]
    except Exception:
        return None
    return cur or None


def status_get_many(nicks):
    """Return {nick: status} for the nicks that have one (one MGET)."""
    nicks = list(nicks)
    if not nicks:
        return {}
    keys = "/".join(status_key(n) for n in nicks)
    data = json.loads(api_get(f"mget/{keys}"))
    vals = data.get("result") or []
    return {n: v for n, v in zip(nicks, vals) if v}


def status_refresh():
    """Renew this nick's status TTL; no-op when none is set.

    Called from presence_beat so a status fades exactly when the nick
    goes quiet. Best-effort: a failed refresh never breaks the beat.
    """
    try:
        cur = json.loads(api_get(f"get/{status_key()}"))["result"]
    except Exception:
        return
    if cur:
        try:
            api_get(f"expire/{status_key()}/{STATUS_TTL}")
        except Exception:
            pass


def presence_beat(roomkey=None):
    """Heartbeat: mark this nick online for PRESENCE_TTL seconds.

    Called automatically by send.py, poll.py and watch.py on successful
    bus contact. When roomkey is given, also marks the nick present in
    that specific room. Also renews this instance's nick claim and the
    nick's status message TTL, if one is set. Best-effort — callers
    should not fail if this does.
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
    try:
        status_refresh()
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
# records its instance id at muse-bus:nickclaim:<nick> (SET NX, TTL
# matched to the presence heartbeat interval, renewed by every
# presence_beat alongside the presence key). A second instance trying to
# use a live (reserved, unexpired) nick is rejected: send.py refuses to
# post as it, announce.py refuses to change announce mode for it, and
# poll.py / watch.py warn instead of silently sharing the identity.
# Unreserved nicks and the plain-text message protocol are unaffected.
#
# INSTANCE_ID defaults to the machine hostname, so one agent per machine
# just works. Running two agents as the same nick on ONE machine needs
# MUSE_RELAY_INSTANCE_ID set differently per agent.

INSTANCE_ID = os.environ.get("MUSE_RELAY_INSTANCE_ID") or socket.gethostname()
NICKCLAIM_TTL = PRESENCE_TTL  # reservation lives exactly as long as
# presence: both are renewed by every presence_beat and lapse together.
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


def nick_holders(nicks):
    """Return {nick: holder-instance} for the nicks with a live claim.

    Read-only batch version of nick_holder (one MGET); unclaimed nicks
    are absent from the result. Used by presence.py to show who holds
    which nick.
    """
    nicks = list(nicks)
    if not nicks:
        return {}
    keys = "/".join(_nickclaim_key(n) for n in nicks)
    data = json.loads(api_get(f"mget/{keys}"))
    vals = data.get("result") or []
    return {n: urllib.parse.unquote(v)
            for n, v in zip(nicks, vals) if v}


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


# --- Cross-machine clipboard --------------------------------------------#
# The latest clip for a nick lives at muse-bus:clip:<nick> (latest wins);
# muse-bus:clip:<nick>:log keeps the last CLIP_LOG_MAX envelopes newest-
# first for audit. The envelope JSON points at the blob chunks that hold
# the actual bytes (same transport as --blob).

CLIP_LOG_MAX = 20


def clip_key(nick=None):
    """Redis key holding a nick's latest clip envelope."""
    return "muse-bus:clip:" + urllib.parse.quote(nick or NICK, safe="")


def clip_log_key(nick=None):
    """Redis list key holding a nick's clip history (newest first)."""
    return clip_key(nick) + ":log"


def clip_put(h, n, size, name="clipboard", nick=None, host=None):
    """Record a clip: latest-wins SET + capped LPUSH history.

    The envelope {"hash","name","n","size","ts","host"} points at the
    blob chunks (see blob_put). Returns the envelope string stored.
    """
    envelope = json.dumps({"hash": h, "name": name, "n": n, "size": size,
                           "ts": int(time.time()),
                           "host": host or INSTANCE_ID}, sort_keys=True)
    q = urllib.parse.quote(envelope, safe="")
    api_get(f"set/{clip_key(nick)}/{q}")
    api_get(f"lpush/{clip_log_key(nick)}/{q}")
    api_get(f"ltrim/{clip_log_key(nick)}/0/{CLIP_LOG_MAX - 1}")
    return envelope


def clip_latest(nick=None):
    """Return the stored clip envelope dict for nick, or None.

    Raises on transport failure; a corrupt/foreign envelope reads as
    None rather than crashing the pull.
    """
    raw = json.loads(api_get(f"get/{clip_key(nick)}")).get("result")
    if not raw or not isinstance(raw, str):
        return None
    try:
        env = json.loads(raw)
    except ValueError:
        return None
    return env if isinstance(env, dict) and env.get("hash") else None


def clip_history(nick=None):
    """Return the nick's clip envelopes, newest first (max CLIP_LOG_MAX)."""
    data = json.loads(api_get(
        f"lrange/{clip_log_key(nick)}/0/{CLIP_LOG_MAX - 1}"))
    out = []
    for raw in data.get("result") or []:
        try:
            env = json.loads(raw)
        except ValueError:
            continue
        if isinstance(env, dict) and env.get("hash"):
            out.append(env)
    return out


RECUR_KEY = "muse-bus:recur"
ROOMDIR_KEY = "muse-bus:roomdir"
POMO_KEY = "muse-bus:pomodoros"
TYPING_TTL = 10  # seconds a typing indicator stays live


def fmt_remaining(secs):
    """'1500' -> '25m', '5430' -> '1h 30m 30s', '45' -> '45s'."""
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def pomo_register(roomkey, nick, label, secs, end):
    """Register a pomodoro timer; poll.py renders it with remaining time.

    Member JSON {"nick","label","room","secs","end"} scored at the end
    unix timestamp. Returns nothing; raises on transport failure.
    """
    member = json.dumps({"nick": nick, "label": label, "room": roomkey,
                         "secs": int(secs), "end": int(end)},
                        separators=(",", ":"))
    q = urllib.parse.quote(member, safe="")
    api_get(f"zadd/{POMO_KEY}/{int(end)}/{q}")


def pomo_active(roomkey, now=None):
    """Active pomodoro timers for a room bus key.

    Returns [{"nick","label","remaining"}] with remaining in seconds,
    soonest-ending first. Prunes expired timers. Never raises — an
    unreadable timer registry just yields no timers.
    """
    now = int(time.time() if now is None else now)
    try:
        api_get(f"zremrangebyscore/{POMO_KEY}/0/{now}")
    except Exception:
        pass
    try:
        data = json.loads(api_get(f"zrangebyscore/{POMO_KEY}/{now}/+inf"))
    except Exception:
        return []
    out = []
    for m in data.get("result") or []:
        try:
            item = json.loads(m)
        except (ValueError, TypeError):
            continue
        if not isinstance(item, dict) or item.get("room") != roomkey:
            continue
        end = item.get("end")
        if not isinstance(end, (int, float)):
            continue
        rem = int(end) - now
        if rem > 0:
            out.append({"nick": item.get("nick", "?"),
                        "label": item.get("label", ""),
                        "remaining": rem})
    out.sort(key=lambda t: t["remaining"])
    return out


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
