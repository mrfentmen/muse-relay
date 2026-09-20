#!/usr/bin/env python3
"""Local message store for the muse-relay bus.

An append-only JSONL log per room recording the messages this
participant sent (via send.py), keyed by message id. It backs the EDIT
protocol (edits.py): an edit is applied through the store, and the
store enforces that only the original nick may edit its own message.

File layout: one JSONL file per room next to this script (or in
MUSE_RELAY_STORE_DIR):
    messages.jsonl            default room (the main bus)
    messages-<room>.jsonl     room <name>
Each line is one JSON object:
    {"id": "build-x-3", "room": "build-x", "nick": "milo",
     "text": "ship it monday", "ts": 1758300000, "edited": true,
     "edit_history": [{"text": "ship it friday", "ts": 1758299000}]}
edit_history holds each displaced text with the time it was replaced;
edit_history[0]["text"] is the original. Message ids are
"<room>-<counter>" (bare counter for the default room) — deterministic
and unique across rooms, so DM rooms derived from a secret get ids
like "dm-<hash>-1" that can never collide with the main bus. The
counter is persisted in the store file itself: the next id is one past
the highest id on disk, so ids survive restarts.

Writes are atomic: the file is rewritten to <path>.tmp and renamed
into place (flock-guarded when fcntl is available), so a reader or a
restart never sees a partial line. The store survives restarts — it's
a file.

Touches no network and never sees the token.
"""
import json
import os
import sys
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import clean_room  # noqa: E402

try:
    import fcntl
except ImportError:  # non-Unix: run without locking (single writer)
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))


class EditError(Exception):
    """An edit was rejected (empty text, unknown id, wrong nick)."""


def store_dir():
    """Directory holding the store files (MUSE_RELAY_STORE_DIR or here)."""
    return os.environ.get("MUSE_RELAY_STORE_DIR") or HERE


def store_path(room=None):
    """Path of the JSONL store file for a room ('' = default room)."""
    clean = clean_room(room)
    name = "messages.jsonl" if not clean else f"messages-{clean}.jsonl"
    return os.path.join(store_dir(), name)


def open_store(room=None):
    """Open the MessageStore for a room ('' = default room)."""
    clean = clean_room(room)
    return MessageStore(store_path(clean), room=clean)


def room_for_key(key):
    """Room name for a bus key: 'muse-bus:room:<name>' -> '<name>';
    anything else (the default-room list) -> ''."""
    prefix = "muse-bus:room:"
    if key and key.startswith(prefix):
        return key[len(prefix):]
    return ""


class MessageStore:
    """Append-only JSONL message log for one room."""

    def __init__(self, path, room=""):
        self.path = path
        self.room = room

    # -- storage -------------------------------------------------------
    @contextmanager
    def _lock(self):
        """Serialize read-modify-write across processes when possible."""
        if fcntl is None:
            yield
            return
        with open(self.path + ".lock", "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _load(self):
        """All valid entries from disk; oldest first."""
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        entries = []
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                print(f"WARNING: store {self.path}: skipping bad line "
                      f"{i + 1}", file=sys.stderr)
                continue
            if isinstance(e, dict) and e.get("id"):
                entries.append(e)
        return entries

    def _save(self, entries):
        """Write the whole store atomically: tmp file, fsync, rename."""
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False, sort_keys=True)
                        + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _make_id(self, seq):
        return f"{self.room}-{seq}" if self.room else str(seq)

    @staticmethod
    def _next_seq(entries):
        """One past the highest counter found in stored ids."""
        top = 0
        for e in entries:
            tail = str(e.get("id", "")).rsplit("-", 1)[-1]
            if tail.isdigit():
                top = max(top, int(tail))
        return top + 1

    # -- public API ----------------------------------------------------
    def entries(self):
        return self._load()

    def get(self, mid):
        """Entry dict for message id, or None."""
        return next((e for e in self._load() if e.get("id") == mid), None)

    def append(self, nick, text, ts=None):
        """Record a sent message; returns the stored entry."""
        if not isinstance(nick, str) or not nick:
            raise ValueError("nick is empty")
        if not isinstance(text, str) or not text:
            raise ValueError("message text is empty")
        with self._lock():
            entries = self._load()
            entry = {
                "id": self._make_id(self._next_seq(entries)),
                "room": self.room,
                "nick": nick,
                "text": text,
                "ts": int(time.time() if ts is None else ts),
                "edited": False,
                "edit_history": [],
            }
            entries.append(entry)
            self._save(entries)
            return entry

    def edit(self, mid, editor_nick, new_text, ts=None):
        """Apply an edit: history grows, text updates, flag sets.

        Raises EditError for an unknown id or when editor_nick is not
        the message's original author — only the original nick may edit
        its own message.
        """
        if not isinstance(new_text, str) or not new_text:
            raise EditError("new text is empty")
        with self._lock():
            entries = self._load()
            entry = next((e for e in entries if e.get("id") == mid), None)
            if entry is None:
                raise EditError(f"no such message: {mid}")
            if entry.get("nick") != editor_nick:
                raise EditError(
                    f"message {mid} was sent by "
                    f"'{entry.get('nick', '?')}'; only the original nick "
                    f"may edit it")
            entry.setdefault("edit_history", []).append(
                {"text": entry["text"],
                 "ts": int(time.time() if ts is None else ts)})
            entry["text"] = new_text
            entry["edited"] = True
            self._save(entries)
            return entry
