#!/usr/bin/env python3
"""Post this machine's health stats to the bus.

Usage: health.py [--room NAME] [--nick NICK]

Sends "<nick>: HEALTH cpu=12% mem=45% disk=70% up=3d4h12m load=0.42"
through send.py's normal send path (idempotency guard, trim, presence).
Room defaults to 'status'.

Dependency-free: CPU from /proc/stat (two samples 0.5s apart, falling
back to loadavg scaled by cpu count), memory from /proc/meminfo, disk
from shutil, uptime from /proc/uptime, load from os.getloadavg.
Never prints the token.
"""
import argparse
import os
import shutil
import sys
import time


def cpu_percent():
    def snap():
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
        return sum(vals), vals[3] + vals[4]  # total, idle+iowait
    total0, idle0 = snap()
    time.sleep(0.5)
    total1, idle1 = snap()
    dt, di = total1 - total0, idle1 - idle0
    if dt <= 0:
        raise ValueError("no cpu time elapsed between samples")
    return 100.0 * (1.0 - di / dt)


def mem_percent():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            info[k] = int(v.split()[0])
    return 100.0 * (1.0 - info["MemAvailable"] / info["MemTotal"])


def disk_percent():
    u = shutil.disk_usage("/")
    return 100.0 * u.used / u.total


def uptime_str():
    secs = int(float(open("/proc/uptime").read().split()[0]))
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, _ = divmod(secs, 60)
    s = f"{m}m"
    if h or d:
        s = f"{h}h" + s
    if d:
        s = f"{d}d" + s
    return s


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default="status",
                    help="room to post to (default: status)")
    ap.add_argument("--nick", default=None,
                    help="nick to post as (default: MUSE_RELAY_NICK)")
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    if args.nick:
        # Set before relay_common is first imported so NICK applies.
        os.environ["MUSE_RELAY_NICK"] = args.nick
    import relay_common  # noqa: E402
    import send  # noqa: E402
    if args.nick:
        # Also cover the already-imported case (e.g. tests in one process).
        relay_common.NICK = args.nick
        send.NICK = args.nick
    try:
        room = relay_common.clean_room(args.room)
        key = relay_common.bus_key(room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        cpu = cpu_percent()
    except Exception:
        try:
            cpu = min(100.0, 100.0 * os.getloadavg()[0] / (os.cpu_count() or 1))
        except Exception as e:
            print(f"ERROR: can't read cpu stats ({e})", file=sys.stderr)
            return 2
    try:
        mem = mem_percent()
        disk = disk_percent()
        up = uptime_str()
        load = os.getloadavg()[0]
    except Exception as e:
        print(f"ERROR: can't read system stats ({e})", file=sys.stderr)
        return 2

    text = (f"HEALTH cpu={cpu:.0f}% mem={mem:.0f}% disk={disk:.0f}% "
            f"up={up} load={load:.2f}")
    return send.post_message(text, key)


if __name__ == "__main__":
    sys.exit(main())
