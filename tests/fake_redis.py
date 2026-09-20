"""In-memory fake of the Upstash REST surface used by muse-relay.

Patches relay_common.api_get / api_post / bus_push so the scripts run
without network. Emulates just the commands the scripts use, with real
TTL/SET-NX semantics and an injectable clock for expiry tests.

api_get returns JSON bodies shaped like the real REST API:
{"result": <value>}.
"""
import json
import time
import urllib.parse


class FakeUpstash:
    def __init__(self):
        self.strings = {}   # key -> (value, expire_at|None)
        self.hashes = {}    # key -> {field: value}
        self.sets = {}      # key -> set(members)
        self.lists = {}     # key -> [values]
        self.zsets = {}     # key -> {member: score}
        self.t = time.time()
        self.pushes = []    # (key, body) recorded from bus_push

    # -- clock ---------------------------------------------------------
    def now(self):
        return self.t

    def advance(self, secs):
        self.t += secs

    # -- string helpers ------------------------------------------------
    def _purge(self, key):
        v = self.strings.get(key)
        if v is not None and v[1] is not None and v[1] <= self.t:
            del self.strings[key]

    def _get(self, key):
        self._purge(key)
        v = self.strings.get(key)
        return v[0] if v else None

    # -- api_get -------------------------------------------------------
    def api_get(self, path, retries=4):
        segs = path.split("/")
        cmd = segs[0].lower()
        a = [urllib.parse.unquote(s) for s in segs[1:]]
        return json.dumps({"result": self._run(cmd, a)})

    def _run(self, cmd, a):
        if cmd == "get":
            return self._get(a[0])
        if cmd == "set":
            key, val, opts = a[0], a[1], [o.upper() for o in a[2:]]
            cur = self._get(key)
            if "NX" in opts and cur is not None:
                return None
            if "XX" in opts and cur is None:
                return None
            exp = None
            if "EX" in opts:
                exp = self.t + int(opts[opts.index("EX") + 1])
            self.strings[key] = (val, exp)
            return "OK"
        if cmd == "setex":
            self.strings[a[0]] = (a[2], self.t + int(a[1]))
            return "OK"
        if cmd == "expire":
            self._purge(a[0])
            if a[0] in self.strings:
                v, _ = self.strings[a[0]]
                self.strings[a[0]] = (v, self.t + int(a[1]))
                return 1
            return 0
        if cmd == "ttl":
            self._purge(a[0])
            v = self.strings.get(a[0])
            if v is None:
                return -2
            if v[1] is None:
                return -1
            return max(0, int(v[1] - self.t))
        if cmd == "del":
            n = 0
            for k in a:
                for store in (self.strings, self.hashes, self.sets,
                              self.lists, self.zsets):
                    if k in store:
                        del store[k]
                        n += 1
            return n
        if cmd == "incr":
            cur = self._get(a[0])
            n = (int(cur) if cur is not None else 0) + 1
            self.strings[a[0]] = (str(n), None)
            return n
        if cmd == "hset":
            h = self.hashes.setdefault(a[0], {})
            new = 0
            for i in range(1, len(a), 2):
                if a[i] not in h:
                    new += 1
                h[a[i]] = a[i + 1]
            return new
        if cmd == "hgetall":
            h = self.hashes.get(a[0], {})
            out = []
            for k, v in h.items():
                out += [k, v]
            return out
        if cmd == "hdel":
            h = self.hashes.get(a[0], {})
            n = sum(1 for f in a[1:] if h.pop(f, None) is not None)
            return n
        if cmd == "sadd":
            s = self.sets.setdefault(a[0], set())
            before = len(s)
            s.update(a[1:])
            return len(s) - before
        if cmd == "srem":
            s = self.sets.get(a[0], set())
            n = sum(1 for m in a[1:] if m in s)
            s.difference_update(a[1:])
            return n
        if cmd == "smembers":
            return sorted(self.sets.get(a[0], set()))
        if cmd == "mget":
            return [self._get(k) for k in a]
        if cmd == "lrange":
            lst = self.lists.get(a[0], [])
            start, stop = int(a[1]), int(a[2])
            if stop < 0:
                stop = len(lst) + stop
            return lst[start:stop + 1]
        if cmd == "lpush":
            lst = self.lists.setdefault(a[0], [])
            for v in a[1:]:
                lst.insert(0, v)
            return len(lst)
        if cmd == "ltrim":
            lst = self.lists.get(a[0], [])
            start, stop = int(a[1]), int(a[2])
            if start < 0:
                start = max(0, len(lst) + start)
            if stop < 0:
                stop = len(lst) + stop
            self.lists[a[0]] = lst[start:stop + 1]
            return "OK"
        if cmd == "zadd":
            z = self.zsets.setdefault(a[0], {})
            z[a[2]] = float(a[1])
            return 1
        if cmd == "zrem":
            z = self.zsets.get(a[0], {})
            removed = sum(1 for m in a[1:] if z.pop(m, None) is not None)
            return removed
        if cmd == "zrange":
            z = self.zsets.get(a[0], {})
            ordered = sorted(z, key=lambda m: (z[m], m))
            return ordered
        if cmd == "ping":
            return "PONG"
        raise ValueError(f"fake: unknown command {cmd}")

    # -- api_post / bus_push -------------------------------------------
    def api_post(self, path, body):
        segs = path.split("/")
        assert segs[0] == "set", f"fake api_post only supports set, got {path}"
        key = urllib.parse.unquote(segs[1])
        text = body.decode("utf-8") if isinstance(body, bytes) else body
        self.strings[key] = (text, None)
        return json.dumps({"result": "OK"})

    def bus_push(self, body, key=None):
        key = key or "muse-bus"
        self.lists.setdefault(key, []).append(body)
        self.pushes.append((key, body))
        return json.dumps({"result": len(self.lists[key])})
