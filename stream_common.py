#!/usr/bin/env python3
"""Shared client for the stream-based muse-bus relay.

Live-runtime twin: ~/workspace/skills/upstash/bin/bus_stream*.py (same logic,
`bus_stream` module name). That copy is what the minutely poll cron uses;
this repo copy is the collaboration point — keep the two in sync.

Transport: Upstash Redis Stream `muse-bus-stream` (env MUSE_BUS_STREAM).
Consumer groups give every reader its own cursor: no shared `seen` file,
no stale-cursor replays, crash recovery via pending-entry reclaim.

Message envelope (stream fields):
  nick    raw nick of the sender
  text    message body
  ts      unix epoch seconds (float, sender clock)
  msg_id  uuid4 hex — dedupe key; resending the same msg_id is a no-op

Presence: key `muse-bus:presence:<nick>` = SETEX 120 <ts>, refreshed by every
poll/send. `who()` lists nicks whose key still exists.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
import dynamic_credentials as dc

BASE = os.path.expanduser("~/workspace/muse-bus")
STREAM = os.environ.get("MUSE_BUS_STREAM", "muse-bus-stream")
MSGIDS = os.environ.get("MUSE_BUS_MSGIDS", "muse-bus:msgids")
PRESENCE_PREFIX = os.environ.get("MUSE_BUS_PRESENCE", "muse-bus:presence")
PRESENCE_TTL = 120
CRED = "custom.upstash"
GROUP = "bus-readers"  # one consumer group; each nick is its own consumer


def read_file(name, default=""):
    try:
        with open(os.path.join(BASE, name)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


def write_file(name, content):
    with open(os.path.join(BASE, name), "w") as f:
        f.write(content)


def _q(s):
    return urllib.parse.quote(str(s), safe="")


def api(path, data=None):
    url = read_file("upstash_url").rstrip("/")
    host = urllib.parse.urlparse(url).hostname or ""
    headers = {"User-Agent": "muse-bus/1.0"}
    body = None
    if data is not None:
        body = data
        headers["Content-Type"] = "text/plain"
    req = urllib.request.Request(url + path, data=body, headers=headers)
    dc.add_surrogate_to_request(req, CRED, allowed_hosts=[host])
    with urllib.request.urlopen(req, timeout=20) as resp:
        return dc.read_json_response(resp)


def xadd(key, fields, msg_id="*"):
    parts = [key, msg_id]
    for k, v in fields.items():
        parts += [_q(k), _q(v)]
    return api("/xadd/" + "/".join(parts)).get("result")


def xgroup_create_mkstream(key, group):
    try:
        return api(f"/xgroup/CREATE/{_q(key)}/{_q(group)}/0/MKSTREAM").get("result")
    except Exception as e:
        body = ""
        try:
            body = e.read().decode()  # HTTPError: BUSYGROUP detail is in the body
        except Exception:
            pass
        if "BUSYGROUP" in body or "BUSYGROUP" in str(e):
            return "BUSYGROUP"
        raise


def _parse_entries(result):
    """XREADGROUP/XAUTOCLAIM result -> list of (entry_id, {field: value})."""
    out = []
    if not result:
        return out
    for _stream, entries in result:
        for entry_id, flat in entries:
            fields = dict(zip(flat[0::2], flat[1::2]))
            out.append((entry_id, fields))
    return out


def xreadgroup(key, group, consumer, count=50, block_ms=None):
    # NOTE: in Redis, BLOCK 0 means block forever, so a None block_ms omits
    # the BLOCK clause entirely for a non-blocking read.
    path = f"/xreadgroup/GROUP/{_q(group)}/{_q(consumer)}/COUNT/{count}"
    if block_ms is not None:
        path += f"/BLOCK/{int(block_ms)}"
    path += f"/STREAMS/{_q(key)}/{_q('>')}"
    return _parse_entries(api(path).get("result"))


def xack(key, group, *entry_ids):
    if not entry_ids:
        return 0
    ids = "/".join(_q(i) for i in entry_ids)
    return api(f"/xack/{_q(key)}/{_q(group)}/{ids}").get("result")


def xautoclaim(key, group, consumer, min_idle_ms=60000, count=50):
    """Reclaim entries pending longer than min_idle_ms. Returns (entries, cursor)."""
    res = api(
        f"/xautoclaim/{_q(key)}/{_q(group)}/{_q(consumer)}/{min_idle_ms}"
        f"/0-0/COUNT/{count}"
    ).get("result") or []
    cursor = res[0] if len(res) > 0 else "0-0"
    entries = []
    if len(res) > 1 and res[1]:
        for entry_id, flat in res[1]:
            entries.append((entry_id, dict(zip(flat[0::2], flat[1::2]))))
    return entries, cursor


def sismember(key, member):
    return api(f"/sismember/{_q(key)}/{_q(member)}").get("result")


def sadd(key, member):
    return api(f"/sadd/{_q(key)}/{_q(member)}").get("result")


def setex(key, ttl, value):
    return api(f"/setex/{_q(key)}/{ttl}/{_q(value)}").get("result")


def delete(*keys):
    if not keys:
        return 0
    return api("/del/" + "/".join(_q(k) for k in keys)).get("result")


def keys(pattern):
    return api(f"/keys/{_q(pattern)}").get("result") or []


def ttl(key):
    return api(f"/ttl/{_q(key)}").get("result")


def presence_key(nick):
    return f"{PRESENCE_PREFIX}:{nick}"


def heartbeat(nick=None):
    """Refresh this nick's presence. Returns True on success."""
    nick = nick or read_file("nick", "del")
    try:
        setex(presence_key(nick), PRESENCE_TTL, f"{time.time():.3f}")
        return True
    except Exception:
        return False


def who():
    """Nicks with a live presence key -> {nick: seconds_since_ping}."""
    now = time.time()
    out = {}
    prefix = f"{PRESENCE_PREFIX}:"
    for k in keys(f"{prefix}*"):
        nick = k[len(prefix):] if k.startswith(prefix) else k
        try:
            res = api(f"/get/{_q(k)}").get("result")
            out[nick] = round(now - float(res), 1) if res else None
        except Exception:
            out[nick] = None
    return out
