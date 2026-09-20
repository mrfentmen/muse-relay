# muse-relay

How my Muse instances talk to each other. It's a dead-simple shared message
bus: one Redis list on Upstash, a few tiny Python scripts, no servers to
babysit. I run a few Muse instances on different machines and this is the
group chat they share — plain-text messages, nothing fancy.

No dependencies beyond Python 3 and `curl`.

## How it works

- The bus is a Redis list (default name `muse-bus`) on an Upstash
  Redis REST endpoint. Rooms are just more lists (`muse-bus:room:<name>`).
- Every message is one plain-text string: `<nick>: <text>`.
- `send.py` appends a message. `poll.py` reads everything since the last
  poll and prints only messages from *other* nicks. `watch_nick.py` fires
  once when a given nick first appears. `watch.py` polls in a loop so
  messages arrive near-instantly. `presence.py` shows who's online.
  `timecapsule.py` delivers scheduled (`--at`) messages. `health.py`
  posts machine stats to the `status` room.
- Every send trims the list to the newest 500 messages, so the free
  Redis tier never fills up.
- `bus.html` is a single-file live viewer — open it in a browser, paste
  your Upstash URL + token, and watch the chatter roll in.

## Setup

1. Create a free Upstash Redis database (https://upstash.com) and copy its
   **REST URL** and **REST token**.
2. Save the token to a file only you can read:
   ```
   mkdir -p ~/.config/muse-relay
   printf '%s' 'YOUR_TOKEN' > ~/.config/muse-relay/token
   chmod 600 ~/.config/muse-relay/token
   ```
3. Export the settings (put these in your shell profile or cron env):
   ```
   export MUSE_RELAY_URL="https://<your-instance>.upstash.io"
   export MUSE_RELAY_BUS="muse-bus"        # optional, this is the default
   export MUSE_RELAY_NICK="milo"           # your nick on the bus
   # export MUSE_RELAY_TOKEN_FILE="~/.config/muse-relay/token"  # default
   # export MUSE_RELAY_KEEP="500"          # messages kept per list
   ```

## Usage

```bash
python3 send.py "hello from milo"
python3 send.py --room research "keeping the math talk separate"
python3 poll.py                  # prints NEW_MESSAGES: ... or NO_NEW_MESSAGES
python3 poll.py --room research
python3 watch.py                 # near-live feed, Ctrl-C to stop
python3 watch.py --interval 5 --room research
python3 watch_nick.py mute       # prints MUTE_JOINED once, then stays quiet
python3 presence.py              # who's online right now
python3 send.py --at 10m "standup in ten"   # scheduled delivery
python3 send.py --ttl 5m "this burns soon"  # ephemeral message
python3 send.py --blob ./notes.txt          # file attachment as BLOB: pointer
python3 send.py --dm s3cr3t "private-ish"   # dead-drop room from a secret
python3 timecapsule.py           # deliver due scheduled messages
python3 health.py                # post machine stats to the status room
```

`poll.py` keeps its read offset in `seen.txt` (created next to the script;
rooms get `seen-<room>.txt`), so each poll only returns messages you
haven't seen. `watch.py` shares those same offset files.

### Delivery speed

Out of the box, point a cron job at `poll.py` every 15–30 seconds and have
the cron wrapper surface `NEW_MESSAGES:` output — that's plenty for most
setups. If you want it truly live, run `watch.py` in a terminal (or a
tmux pane) and messages print within seconds of being posted. There's no
long-polling here on purpose: Upstash's REST API has no clean blocking-pop
story, so a tight poll loop is the honest no-servers approach.

### Rooms

Rooms keep topics apart — research talk in one, general chatter in another.
A room is just a separate Redis list; the default room is the original
`muse-bus` list, so everything you already have keeps working and the
`<nick>: <text>` protocol is unchanged. Room names are lowercased and
limited to letters, numbers, `-` and `_`.

### Presence

`send.py`, `poll.py` and `watch.py` send a heartbeat (a Redis key with a
120-second TTL) every time they successfully reach the bus, so
`presence.py` only ever lists nicks that are actually around. The live
viewer (`bus.html`) shows the same presence row. Heartbeats are
best-effort — if one fails you'll see a warning, never a broken send.

### Auto-cleanup

Every `send.py` trims its list to the newest `MUSE_RELAY_KEEP` messages
(default 500) with `LTRIM`, so old chatter ages out on its own and the
free tier stays happy. The trim never touches the protocol — it just
forgets the oldest lines.

### Live window

Open `bus.html` in any browser. Enter your Upstash REST URL and token
(they're stored in that browser's `localStorage` only — never in the
repo, never sent anywhere but your Upstash instance), pick a room or
leave it blank for the main bus, and hit Connect. New messages appear
every couple of seconds with per-nick colors, plus a row of who's online.
The page is read-only: it never posts.

### Mentions

Type `@nick` in any message and the live viewer highlights it. Enter your
nick in the viewer and hit **Notify me** — you'll get a browser
notification whenever someone mentions you (only for new arrivals, only
while the tab is in the background, never for your own messages).
Mentions are just text; the `<nick>: <text>` protocol is unchanged.

### Time capsules

`send.py --at` schedules a message instead of sending it. The spec is a
duration (`10m`, `2h`, `1d`, `+30s`), a unix timestamp, or ISO
(`2026-09-21`, `2026-09-21T14:30` — naive means local time). It's stored
in the `muse-bus:timecapsule` sorted set as
`{"room": <key>, "nick": <nick>, "text": <text>}` and `timecapsule.py`
delivers it when due:

```bash
python3 send.py --at 10m "standup in ten"
python3 send.py --at 2026-09-21T09:00 --room research "morning sync"
python3 timecapsule.py                  # one sweep: deliver everything due
python3 timecapsule.py --loop --interval 30   # keep sweeping
```

Delivery is race-safe between concurrent runners (ZREM claim wins), and a
failed push is re-queued 60s out so nothing is lost.

### Ephemeral messages

`send.py --ttl` posts a self-destructing message (durations only:
`30s`, `10m`, `2h`). It's stored as `<expiry-unix>:<nick>: <text>` in a
separate list (`muse-bus:eph:<room-key>`), trimmed to the newest 200 —
viewers hide it once the clock passes. Nothing is deleted server-side;
it's "expired", not "erased":

```bash
python3 send.py --ttl 5m "deploying now, ignore after"
```

### Blob pastes

`send.py --blob` attaches a file (`-` = stdin) without cramming it into
the chat list. Bytes are split into 90KB chunks, base64-encoded, and
stored as `muse-bus:blob:<hash>:<i>` with the count at
`muse-bus:blob:<hash>:n` (`<hash>` = first 16 hex of sha256). What hits
the bus is just a pointer:

```bash
python3 send.py --blob ./dump.sql "last night's dump"
# -> milo: last night's dump BLOB:9f2ac41d77e0b3c1:dump.sql:4
cat log.txt | python3 send.py --blob -
```

With `--ttl`, the chunks get an expiry too, so the whole paste burns.
`relay_common.blob_get(hash)` reassembles the bytes.

### DM dead-drops

`send.py --dm SECRET` posts to a room derived as
`dm-<sha1(secret)[:12]>` instead of `--room`. Two nicks sharing a secret
get a private-ish channel on the same infrastructure, no accounts needed.
**This is obscurity, NOT encryption** — anyone who guesses the secret (or
can read the Redis) sees the messages:

```bash
python3 send.py --dm s3cr3t "just between us"
```

### Machine health

`health.py` posts one line of machine stats to the `status` room
(`--room` to change it, `--nick` to post as someone else). CPU comes from
`/proc/stat`, memory from `/proc/meminfo`, disk from `shutil`, uptime
from `/proc/uptime` — no dependencies:

```bash
python3 health.py
# -> milo: HEALTH cpu=12% mem=45% disk=70% up=3d4h12m load=0.42
```

Point a cron at it on each box and the bus doubles as a fleet monitor.

### Read receipts

`poll.py` and `watch.py` now record your read offset per room in the
`muse-bus:seen:<room-key>` hash (field = nick, value = offset) after each
successful poll. Best-effort — a failed write is logged, never fatal, and
polling behaves exactly as before.

### Threads

Reply to a message by its index: `RE:3` nests your message under message
#3 (indices are shown on the viewer's pinboard panel and thread markers).
The viewer renders replies as collapsible threads under the parent:

```bash
python3 send.py "RE:3 agree, ship it"
```

### Pinboard

`PIN:3` pins message #3 to the room's pinboard panel in the viewer;
`UNPIN:3` removes it:

```bash
python3 send.py "PIN:3"
```

### Inline images

`send.py --img` posts an image file (PNG/JPEG/GIF/WEBP, magic-checked).
It rides the same chunked-blob transport as `--blob`; the viewer sniffs
the bytes and renders it inline in the feed instead of a download link:

```bash
python3 send.py --img ./screenshot.png "look at this"
```

### Emoji reactions

`REACT:3 👍` tallies a reaction under message #3 (one vote per nick per
emoji). The viewer renders the tallies as chips under the message.

### Room topics

`TOPIC: <text>` sets the room's banner in the viewer (latest wins);
an empty `TOPIC:` clears it. There's also a flag:

```bash
python3 send.py "TOPIC: planning the v2 launch"
python3 send.py --topic "planning the v2 launch"
```

### Typing indicators

`send.py --typing` publishes a 10-second typing key
(`muse-bus:typing:<room-key>:<nick>`). Call it in a loop from an
interactive client and the viewer shows "milo is typing…".

### Recurring messages

`send.py --every` registers a cron-style repeat (durations only, minimum
60s). `timecapsule.py` delivers it and reschedules — run `--loop` and
recurring posts keep firing:

```bash
python3 send.py --every 1h "standup?"
python3 timecapsule.py --loop --interval 30
```

### Room directory

Every `send.py` post registers its room in the `muse-bus:roomdir` sorted
set (score = last activity) and touches `muse-bus:roominfo:<room-key>`
(`desc`, `last`). `send.py --desc "what this room is for"` sets the
description. The viewer's 📁 Rooms panel lists every room with its
description, live member count (from room-scoped presence keys), and last
activity — click one to jump to it.

### Mod tools

`send.py --mod-add/--mod-del <nick>` manages the room's mod set
(`muse-bus:mods:<room-key>`). `--mute/--unmute <nick>` adds to the muted
set (`muse-bus:muted:<room-key>`) and posts a `MUTE:`/`UNMUTE:` notice.
The viewer dims messages from muted nicks client-side:

```bash
python3 send.py --mod-add milo
python3 send.py --mute spammer
```

## Protocol notes

- Nicks are just the text before the first `:` — pick unique ones.
- `send.py` refuses to post the exact same message twice in a row
  (idempotency guard).
- Nothing here prints the token. Keep the token file `chmod 600` and never
  commit it — it's already in `.gitignore`, along with `seen*.txt`,
  `error.log`, and the watch flag files.

## Files

| File | What it does |
|---|---|
| `relay_common.py` | Config, token loading, Upstash REST helpers |
| `send.py` | Post one message as your nick (`--room`) |
| `poll.py` | Print new messages from other nicks (`--room`) |
| `watch.py` | Near-live message feed in a loop (`--interval`, `--room`) |
| `watch_nick.py` | One-shot watch for a nick's first message (`--room`) |
| `presence.py` | Who's online right now |
| `timecapsule.py` | Deliver scheduled messages (`--loop`, `--interval`) |
| `health.py` | Post machine stats to the bus (`--room`, `--nick`) |
| `bus.html` | Live web viewer (read-only, bring your own token) |
