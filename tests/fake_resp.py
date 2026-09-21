"""A tiny RESP2 server, enough for one subscriber to connect to.

`resp.Subscriber` speaks to a real socket, so testing it against a mock
object would test nothing but the mock. This is a real TCP server on
127.0.0.1 that implements exactly the commands a subscriber uses —
AUTH, SUBSCRIBE, PING, PUBLISH — and can push frames to everyone
subscribed to a channel. No TLS (a test needs no certificate; the client
takes plain redis:// URLs for local servers).

The store is exported so a test can bridge two transports: an HTTP REST
fake can call `server.publish(channel, message)` and the message lands on
a real socket in another thread, which is how the end-to-end test proves
`send.py` actually wakes a listener.
"""
import socket
import threading


class FakeRespServer:
    """One subscriber-friendly RESP2 server on an ephemeral port."""

    def __init__(self, password=None):
        self.password = password
        self.host = "127.0.0.1"
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._clients = []          # [{"sock", "channels", "authed", lock}]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.threads = []
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               daemon=True)
        self._accept_thread.start()

    # -- lifecycle ------------------------------------------------------
    @property
    def url(self):
        """redis:// URL for this server (with auth when it has a password)."""
        if self.password:
            return f"redis://user:{self.password}@{self.host}:{self.port}"
        return f"redis://{self.host}:{self.port}"

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            try:
                c["sock"].close()
            except OSError:
                pass

    # -- publishing -----------------------------------------------------
    def publish(self, channel, message):
        """Push one message to every subscriber of `channel`.

        Returns the number of clients that were listening, like PUBLISH.
        """
        frame = (_bulk("message") + _bulk(channel)
                 + _bulk(message))
        frame = b"*3\r\n" + frame
        with self._lock:
            clients = [c for c in self._clients
                       if channel in c["channels"]]
        n = 0
        for c in clients:
            try:
                c["sock"].sendall(frame)
                n += 1
            except OSError:
                pass
        return n

    def subscriber_count(self, channel=None):
        """How many clients are subscribed (to `channel` when given)."""
        with self._lock:
            if channel is None:
                return len(self._clients)
            return sum(1 for c in self._clients if channel in c["channels"])

    def wait_for_subscriber(self, channel, timeout=5.0):
        """Block until someone subscribes to `channel` (test helper)."""
        deadline = threading.Event()
        waited = 0.0
        while waited < timeout:
            if self.subscriber_count(channel):
                return True
            deadline.wait(0.02)
            waited += 0.02
        return False

    # -- server internals ----------------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, _ = self._sock.accept()
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(sock,),
                                 daemon=True)
            self.threads.append(t)
            t.start()

    def _serve(self, sock):
        client = {"sock": sock, "channels": [], "authed": self.password is None}
        with self._lock:
            self._clients.append(client)
        buf = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                while True:
                    parsed = _parse(buf, 0)
                    if parsed is None:
                        break
                    consumed, args = parsed
                    del buf[:consumed]
                    if not self._handle(sock, client, args):
                        return
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, sock, client, args):
        """Run one command. Returns False to close the connection."""
        if not args:
            _send(sock, b"-ERR empty command\r\n")
            return True
        cmd = args[0].upper()
        if cmd == "AUTH":
            given = args[-1]
            if self.password is not None and given == self.password:
                client["authed"] = True
                _send(sock, b"+OK\r\n")
            else:
                _send(sock, b"-ERR invalid password\r\n")
                return False
            return True
        if not client["authed"]:
            _send(sock, b"-NOAUTH Authentication required.\r\n")
            return False
        if cmd == "SUBSCRIBE":
            for channel in args[1:]:
                if channel not in client["channels"]:
                    client["channels"].append(channel)
                _send(sock, b"*3\r\n" + _bulk("subscribe") + _bulk(channel)
                      + b":%d\r\n" % len(client["channels"]))
            return True
        if cmd == "PING":
            _send(sock, b"+PONG\r\n")
            return True
        if cmd == "PUBLISH":
            n = self.publish(args[1], args[2])
            _send(sock, b":%d\r\n" % n)
            return True
        if cmd == "QUIT":
            _send(sock, b"+OK\r\n")
            return False
        _send(sock, b"-ERR unknown command '%s'\r\n" % cmd.encode())
        return True


def _bulk(value):
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return b"$" + str(len(raw)).encode() + b"\r\n" + raw + b"\r\n"


def _send(sock, data):
    try:
        sock.sendall(data)
    except OSError:
        pass


def _parse(buf, pos):
    """Parse one RESP array of bulk strings; None when incomplete."""
    if pos >= len(buf) or buf[pos:pos + 1] != b"*":
        return None
    nl = buf.find(b"\r\n", pos)
    if nl < 0:
        return None
    try:
        n = int(bytes(buf[pos + 1:nl]))
    except ValueError:
        raise ValueError("bad array header")
    cur, args = nl + 2, []
    for _ in range(n):
        if cur >= len(buf) or buf[cur:cur + 1] != b"$":
            return None
        nl = buf.find(b"\r\n", cur)
        if nl < 0:
            return None
        try:
            ln = int(bytes(buf[cur + 1:nl]))
        except ValueError:
            raise ValueError("bad bulk header")
        end = nl + 2 + ln
        if len(buf) < end + 2:
            return None
        args.append(bytes(buf[nl + 2:end]).decode("utf-8", "replace"))
        cur = end + 2
    return cur, args
