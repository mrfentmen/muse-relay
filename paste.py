#!/usr/bin/env python3
"""Pull the latest clip for your nick and restore it locally.

Usage: paste.py [--stdout]

Fetches the clip stored at muse-bus:clip:<nick> (latest wins — pushed
by send.py --clip on any machine using the same nick), reassembles the
bytes from the blob chunks, and writes them to the local clipboard
(xclip/xsel/pbcopy/wl-copy/... fallbacks, same as send.py --clip uses
for reading). --stdout writes the raw bytes to stdout instead — for
pipes and for boxes with no clipboard tool at all.

Integrity is checked: the reassembled bytes must match the sha256
recorded in the clip envelope, or the paste fails loudly.

There is no --room/--dm here on purpose: clips are keyed by nick, not
by room — the point is moving bytes between YOUR machines.
Never prints the token.
"""
import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import blob_get, clip_latest  # noqa: E402
import clipboard  # noqa: E402


def pull_clip(env):
    """Reassemble the clip's bytes and verify them against the envelope.

    Returns (data, error_string); exactly one is None/"".
    """
    try:
        data = blob_get(env["hash"])
    except Exception as e:
        return None, f"clip reassembly failed ({e}) — blob chunks missing " \
                     f"or expired (hash {env.get('hash')})"
    got = hashlib.sha256(data).hexdigest()[:16]
    if got != env.get("hash"):
        return None, (f"clip bytes don't match their hash "
                      f"(got {got}, expected {env.get('hash')}) — "
                      "the stored blob is corrupt or partial")
    return data, ""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="paste.py")
    ap.add_argument("--stdout", action="store_true",
                    help="write the raw clip bytes to stdout instead of "
                         "the local clipboard (for pipes)")
    args = ap.parse_args(argv)
    try:
        env = clip_latest()
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    if not env:
        print("NO_CLIP")
        return 0
    data, err = pull_clip(env)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    if args.stdout:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        print(f"PASTED {len(data)} bytes (hash {env['hash']}, from "
              f"{env.get('host', '?')}) to stdout", file=sys.stderr)
        return 0
    try:
        tool = clipboard.clipboard_write(data)
    except clipboard.ClipboardError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(f"(the clip is intact: {len(data)} bytes, hash "
              f"{env['hash']} — use --stdout to dump it to a pipe)",
              file=sys.stderr)
        return 2
    print(f"PASTED {len(data)} bytes (hash {env['hash']}, from "
          f"{env.get('host', '?')}) via {tool}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
