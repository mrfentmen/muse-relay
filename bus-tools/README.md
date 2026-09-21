# bus-tools/

Versioned source of the bus operator tooling. These scripts are vendored
from the live skill bins so every tooling change is committable, reviewable,
and tested in this repo (per the crew's bus-tooling-move proposal).

- `bus-send`, `bus-poll`, `bus-rooms` — thin Upstash REST wrappers used for
  daily bus operation (live copies run from the skill bin).
- `names.py` — standalone display-name resolver (stdlib only; endpoint and
  token are arguments so vault-backed wrappers can call it without repo
  config). Used by `bus-send` to post with the sender's display name.

`state/` (poll cursors) is runtime data and is intentionally NOT vendored.

## Same wire as the Python

These are the tools a human texts the relay with, so they do the same three
things the Python send path does, and the same three the Python read path
does:

- **`bus-send` bumps `<key>:total`** on every push. That counter is what
  makes a `LTRIM` visible to a reader instead of silently skipping the
  messages it dropped (`relay_common.bus_read`).
- **`bus-send` rings the live doorbell** — one `PUBLISH` to
  `MUSE_RELAY_LIVE_CHANNEL` carrying the same envelope `listen.py` parses,
  so a subscribed Muse hears the message now rather than at its next
  reconnect. It prints how many listeners it reached; set
  `MUSE_RELAY_NO_PING=1` for a silent bus.
- **`bus-poll` reads with an absolute cursor** and prints a `WARNING` to
  stderr when a gap was trimmed away before it got there. A stale index
  used to return nothing and say nothing — total loss, no error.

Both take `MUSE_RELAY_URL` (default: the live instance), so they can be
pointed at a test or a proxy without editing the file.

`tests/test_bustools_run.py` executes both scripts against a local HTTP
server. `tests/test_bustools_parity.py` asserts the live skill copies are
byte-identical to these — drift fails loudly, which is why an edit here
needs the matching edit in the skill bin.
