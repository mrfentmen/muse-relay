#!/usr/bin/env python3
"""Minimal RESP2 client that can hold a subscription open over TCP.

Every other module in muse-relay talks to Upstash over its stateless REST
API with curl. SUBSCRIBE is the exception: it is connection-oriented and
Upstash serves it over native TLS TCP, *not* over the REST endpoint (see
the SUBSCRIBE and PUBLISH pages in the Upstash command docs — publishing
works over both, subscribing needs the connection). So this is the one
place in the repo that opens a socket.

The point: a listener learns about a message the moment it is posted,
instead of asking "anything new?" on a timer. An idle open subscription
costs nothing, where a poll loop burns commands for every tick.

Scope is deliberately tiny — connect, AUTH, SUBSCRIBE, read push frames,
PING, close. RESP2 only (no HELLO). Standard library only, like the rest
of the repo. Not a general Redis client: a subscribed RESP2 connection
may only run subscription commands plus PING/RESET/QUIT, so there is
nothing else to implement.

TLS verification is on (`ssl.create_default_context`), so `rediss://...`
URLs authenticate the server. `redis://...` (no TLS) is for local test
servers only — never point it at Upstash.
"""
import socket
import ssl
import time
import urllib.parse

DEFAULT_PORT = 6379
DEFAULT_TIMEOUT = 15.0  # seconds for a handshake or a command reply
READ_SLICE = 1.0        # longest single socket read while waiting


class RespError(RuntimeError):
    """Transport or protocol failure on the subscription socket."""


def parse_tcp_url(url):
    """'rediss://user:pass@host:6379' -> (host, port, user, password, tls).

    Raises RespError for anything that is not a redis:// or rediss:// URL.
    The username Upstash prints in REDIS_URL is 'default', which is also
    what a one-argument AUTH means, so it is reported as '' in that case.
    """
    try:
        parts = urllib.parse.urlparse((url or "").strip())
    except ValueError as e:
        raise RespError(f"can't parse URL: {e}")
    if parts.scheme not in ("redis", "rediss"):
        raise RespError(
            f"not a redis TCP URL (want redis:// or rediss://): {url!r}")
    if not parts.hostname:
        raise RespError(f"URL has no host: {url!r}")
    user = urllib.parse.unquote(parts.username) if parts.username else ""
    if user == "default":
        user = ""
    password = urllib.parse.unquote(parts.password) if parts.password else ""
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError:
        raise RespError(f"URL has a bad port: {url!r}")
    return parts.hostname, port, user, password, parts.scheme == "rediss"


def encode_command(*args):
    """Encode one command as a RESP array of bulk strings."""
    out = bytearray(b"*" + str(len(args)).encode() + b"\r\n")
    for a in args:
        if isinstance(a, str):
            a = a.encode("utf-8")
        elif isinstance(a, int):
            a = str(a).encode()
        out += b"$" + str(len(a)).encode() + b"\r\n" + a + b"\r\n"
    return bytes(out)


_INCOMPLETE = object()


class Subscriber:
    """One open RESP2 subscription with a buffered frame reader.

    Usage:
        sub = Subscriber(url, ["muse-bus:live"])
        sub.connect()
        while True:
            event = sub.next_event(timeout=30)
            if event is None:
                sub.ping()          # keepalive + dead-link detection
                continue
            kind, channel, payload = event

    next_event returns ("message", channel, payload) for a real publish and
    ("pong", "", "") / ("subscribe", channel, count) for bookkeeping, so
    callers decide what they care about. A broken link raises RespError
    (rather than looking like an idle spell) so the caller reconnects.
    """

    def __init__(self, url, channels, read_slice=READ_SLICE):
        if not channels:
            raise RespError("no channels to subscribe to")
        self.url = url
        self.channels = list(channels)
        self.read_slice = read_slice
        self._sock = None
        self._buf = bytearray()
        self._pending = []   # frames read while subscribing

    # -- lifecycle ------------------------------------------------------
    def connect(self):
        """Open the socket, AUTH when needed, SUBSCRIBE, await confirms.

        Returns the number of channels confirmed. Raises RespError on any
        failure, so callers can retry with a backoff.
        """
        self.close()
        host, port, user, password, tls = parse_tcp_url(self.url)
        try:
            sock = socket.create_connection((host, port), timeout=DEFAULT_TIMEOUT)
        except OSError as e:
            raise RespError(f"connect {host}:{port} failed: {e}")
        if tls:
            ctx = ssl.create_default_context()
            try:
                sock = ctx.wrap_socket(sock, server_hostname=host)
            except (ssl.SSLError, OSError) as e:
                sock.close()
                raise RespError(f"TLS handshake with {host} failed: {e}")
        sock.settimeout(self.read_slice)
        self._sock = sock
        self._buf.clear()
        self._pending.clear()
        try:
            if password:
                if user:
                    self._send("AUTH", user, password)
                else:
                    self._send("AUTH", password)
                self._read_frame(DEFAULT_TIMEOUT)   # +OK, or raises
            self._send("SUBSCRIBE", *self.channels)
            confirmed = 0
            deadline = time.monotonic() + DEFAULT_TIMEOUT
            while confirmed < len(self.channels):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RespError("timed out waiting for SUBSCRIBE reply")
                kind, channel, _ = self._read_frame(left)
                if kind == "subscribe":
                    confirmed += 1
                elif kind == "message":
                    # a publish landed mid-handshake; keep it for the caller
                    self._pending.append((kind, channel, _))
                elif kind == "pong":
                    continue
                else:
                    raise RespError(f"unexpected reply to SUBSCRIBE: {kind}")
        except Exception:
            self.close()
            raise
        return confirmed

    def close(self):
        """Close the socket; never raises."""
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # -- commands -------------------------------------------------------
    def ping(self):
        """Send PING (allowed on a subscribed connection)."""
        self._send("PING")

    def _send(self, *args):
        if self._sock is None:
            raise RespError("not connected")
        try:
            self._sock.sendall(encode_command(*args))
        except OSError as e:
            raise RespError(f"send failed: {e}")

    # -- reading --------------------------------------------------------
    def next_event(self, timeout):
        """Next frame within `timeout` seconds, or None when nothing came.

        Returns (kind, channel, payload). Raises RespError when the link
        is broken so the caller can reconnect.
        """
        if self._pending:
            return self._pending.pop(0)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            frame = self._read_frame(left, soft_timeout=True)
            if frame is None:
                return None
            # bookkeeping frames pass through; the caller ignores them
            return frame

    def _read_frame(self, timeout, soft_timeout=False):
        """Read exactly one frame. Returns None only for a soft timeout."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            parsed = self._parse(0)
            if parsed is not _INCOMPLETE:
                consumed, value = parsed
                del self._buf[:consumed]
                return self._classify(value)
            left = deadline - time.monotonic()
            if left <= 0:
                if soft_timeout:
                    return None
                raise RespError("timed out waiting for a reply")
            if self._sock is None:
                raise RespError("not connected")
            self._sock.settimeout(max(0.001, min(self.read_slice, left)))
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as e:
                raise RespError(f"read failed: {e}")
            if not chunk:
                raise RespError("connection closed by peer")
            self._buf.extend(chunk)

    def _parse(self, pos):
        """Parse one RESP value at `pos`.

        Returns (consumed_bytes, value), or _INCOMPLETE when the buffer
        does not hold a whole frame yet. Error frames raise RespError.
        """
        buf = self._buf
        if pos >= len(buf):
            return _INCOMPLETE
        tag = buf[pos:pos + 1]
        if tag in (b"+", b"-", b":"):
            nl = buf.find(b"\r\n", pos + 1)
            if nl < 0:
                return _INCOMPLETE
            raw = bytes(buf[pos + 1:nl]).decode("utf-8", "replace")
            nxt = nl + 2
            if tag == b"+":
                return nxt, raw
            if tag == b"-":
                raise RespError(raw)
            try:
                return nxt, int(raw)
            except ValueError:
                raise RespError(f"bad integer frame: {raw!r}")
        if tag == b"$":
            nl = buf.find(b"\r\n", pos + 1)
            if nl < 0:
                return _INCOMPLETE
            try:
                n = int(bytes(buf[pos + 1:nl]))
            except ValueError:
                raise RespError("bad bulk-string length")
            if n < 0:                      # null bulk string
                return nl + 2, None
            end = nl + 2 + n
            if len(buf) < end + 2:
                return _INCOMPLETE
            return end + 2, bytes(buf[nl + 2:end]).decode("utf-8", "replace")
        if tag == b"*":
            nl = buf.find(b"\r\n", pos + 1)
            if nl < 0:
                return _INCOMPLETE
            try:
                n = int(bytes(buf[pos + 1:nl]))
            except ValueError:
                raise RespError("bad array length")
            if n < 0:                      # null array
                return nl + 2, None
            items, cur = [], nl + 2
            for _ in range(n):
                got = self._parse(cur)
                if got is _INCOMPLETE:
                    return _INCOMPLETE
                cur, item = got
                items.append(item)
            return cur, items
        raise RespError(f"unknown frame type {tag!r}")

    @staticmethod
    def _classify(value):
        """Normalize a parsed frame into (kind, channel, payload)."""
        if isinstance(value, list) and value and isinstance(value[0], str):
            kind = value[0]
            if kind == "message" and len(value) >= 3:
                return kind, value[1] or "", value[2] or ""
            if kind in ("subscribe", "unsubscribe") and len(value) >= 2:
                count = value[2] if len(value) > 2 else 0
                return kind, value[1] or "", count
            if kind in ("pong", "subscribed", "ok"):
                return "pong", "", ""
        if isinstance(value, str) and value.upper() in ("PONG", "OK"):
            return "pong", "", ""
        return "other", "", ""
