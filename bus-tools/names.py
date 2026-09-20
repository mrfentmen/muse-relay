#!/usr/bin/env python3
"""Standalone display-name resolver for the muse-bus names registry.

Usage:
  names.py --endpoint URL --token TOKEN [--prefix P] get <nick>
  names.py --endpoint URL --token TOKEN [--prefix P] all

Prints the display name for a nick (or the nick itself when unset), one per
line for `all` as "<nick>\\t<display>". Exit 0 on success, 2 on transport
failure (fail-open callers then use the nick itself).

Deliberately dependency-free (stdlib only) and env-free: endpoint and token
are arguments so thin wrappers (e.g. the bus-send skill, which holds a
vault surrogate in-process) can call it without repo config.
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request


def _api(endpoint, token, path, timeout=15):
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/" + path,
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": "muse-bus-names/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"NAMES_ERROR: {e}", file=sys.stderr)
        return None


def cmd_get(endpoint, token, prefix, nick):
    data = _api(endpoint, token,
                f"hget/{prefix}:names/{urllib.parse.quote(nick, safe='')}")
    if data is None:
        return 2
    raw = data.get("result")
    if raw:
        try:
            print(json.loads(raw).get("display") or nick)
            return 0
        except Exception:
            pass
    print(nick)
    return 0


def cmd_all(endpoint, token, prefix):
    data = _api(endpoint, token, f"hgetall/{prefix}:names")
    if data is None:
        return 2
    items = data.get("result") or []
    for i in range(0, len(items), 2):
        nick = items[i]
        try:
            disp = (json.loads(items[i + 1]) or {}).get("display") or nick
        except Exception:
            disp = nick
        print(f"{nick}\t{disp}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--prefix", default="muse-bus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("get")
    p.add_argument("nick")
    sub.add_parser("all")
    args = ap.parse_args(argv)
    if args.cmd == "get":
        return cmd_get(args.endpoint, args.token, args.prefix, args.nick)
    return cmd_all(args.endpoint, args.token, args.prefix)


if __name__ == "__main__":
    sys.exit(main())
