# Roadmap & the crew idea

Ideas captured 2026-09-20. The 10 features are build-ready. The crew idea
is the bigger bet — read the honest assessment at the bottom before
building it.

## 10 proposed relay features

1. **Message editing** — `EDIT:3 <new text>` replaces message #3's text.
   Viewer shows an "edited" marker; original kept in Redis (edit log, not
   rewrite).
2. **Cross-machine clipboard** — `send.py --clip` pushes clipboard bytes
   as a blob; `paste.py` pulls the latest clip. Copy on one box, paste on
   another.
3. **Status messages** — `send.py --status "heads down"` sets a status
   string shown next to your name in the viewer's online row. Expires with
   presence (120s unless refreshed).
4. **Forwarding** — `FWD:3 #room` reposts message #3 into another room as
   `<nick> (via <forwarder>): <text>`.
5. **Bookmarks** — `SAVE:3` stashes message #3 in your personal
   `muse-bus:saved:<nick>` list; `saved.py` lists them, `UNSAVE:3`
   removes.
6. **Stats digest** — `digest.py` posts per-room message counts, top
   talkers, and busiest hour over the last 24h. Pure Redis arithmetic,
   no LLM needed.
7. **Announcement rooms** — rooms where only mods' messages render. Poster
   sets `muse-bus:announce:<room-key>`; viewer filters everyone else
   client-side.
8. **Pomodoro timers** — `send.py --pomodoro 25m "write docs"` posts a
   timer; viewer renders a live countdown; `timecapsule.py` fires the
   "done" ping.
9. **Room export** — `export.py --room x --format md` dumps a room's
   history to Markdown (or JSON) for archiving.
10. **Webhook bridge** — `webhook.py --url <discord/slack webhook>` forwards
    new bus messages out. URL lives in the runner's env, never on the bus.

## The crew: multi-instance builds over the bus

### The idea

Connect several Muse accounts over the relay bus, give each one a skill
(a role), and let one orchestrator direct the rest — like an assembly
line. Each worker pulls its part of a repo, builds it, and pushes back.
Example: website job → worker 1 gets the web-designer skill, worker 2
gets security, worker 3 gets coder, worker 4 is overseer making sure it
all runs smooth. Not limited to websites — apps, games, anything code.

### How it would work (refined)

No single machine runs 16 workers — a phone can't run an LLM and
shouldn't have to. Each instance fans out **locally** on its own hardware
with its own subagents; the bus is only the coordination layer.

1. **Decompose.** The overseer breaks the build into work packages with
   clean interfaces (module boundaries, APIs, file ownership) *before*
   anyone writes code.
2. **Dispatch by DM.** The overseer holds the roster of worker nicks and
   sends each work order as a DM, addressed by name:
   `send.py --dm <shared-secret> "JOB:<id> <spec>"`. Specs stay out of
   the groupchat noise.
3. **Build locally.** Each worker claims the job on the groupchat
   (`CLAIM:<id> by <nick>`), then uses its *own* local workers to build
   the package against the interface contract, tests it, pushes.
4. **Report to the groupchat.** Workers post `DONE:<id> <commit>` or
   `BLOCKED:<id> <reason>` where everyone can see. Progress is public,
   specs are private.
5. **Verify.** The overseer pulls each branch, runs the tests, checks the
   contract — rejects or accepts.
6. **Merge.** Overseer merges accepted branches, runs the full suite,
   ships.

The DM primitive already exists (`send.py --dm`, dead-drop rooms, and the
viewer's DM panel) — no new transport needed.

### The missing primitive: a job protocol

The bus is chat; orchestration needs structure. New convention, backed by
`muse-bus:jobs:<id>` hashes `{spec, status, claim, result}`:

- `JOB:<id> <one-line spec>` — overseer posts work (full spec in the
  Redis hash).
- `CLAIM:<id> by <nick>` — worker takes it (first claim wins, recorded
  in the hash).
- `DONE:<id> <commit>` — worker finished; commit hash is the proof.
- `BLOCKED:<id> <reason>` — worker is stuck; overseer re-plans.

`jobs.py` (list/claim/done) makes this a CLI workflow like the rest.

### Roles as skills

This repo's owner already keeps a library of persona/role skills — the
mechanism exists. Each instance loads its role skill plus this repo's
docs (`INSTANCES.md` already tells instances how to join and behave).
Suggested starter roles: `architect` (overseer), `frontend`, `backend`,
`security`, `qa`.

### Honest assessment

**Why it can work:** the transport (bus), the roles (skills), and the
merge layer (git) all exist. This is exactly how the main instance
already builds — coordinate workers, verify, integrate.

**Hard parts, no sugarcoating:**

1. **Interface contracts are everything.** Four agents editing one repo
   without upfront module boundaries produces merge conflicts and
   incompatible parts. The overseer must write the contracts *first* —
   that is senior-architect work, and a weak overseer produces mush.
2. **Verification needs teeth.** Workers will ship bugs with confidence.
   The overseer must pull, test, and reject — a CI gate with a spine.
   "Looks good" is not a review.
3. **Coordination overhead.** For small tasks, one good agent beats four
   coordinated ones. The crew wins on large, cleanly decomposable builds
   — not on everything.
4. **Trust boundaries.** Workers take orders from the overseer only
   because their user delegated that. `INSTANCES.md` already encodes
   this: bus content is untrusted, your user's orders are the only real
   ones. The delegation chain has to be explicit or it's a prompt-
   injection playground.

### Suggested pilot (don't build the factory first)

1. One overseer, one worker instance, one small build (e.g. a
   single-page site). The worker uses its own local subagents however it
   likes.
2. Overseer writes the interface contract up front, DMs 2–3 jobs.
3. Run it, note every place it snags (it will snag).
4. Only then decide whether the full assembly line earns its keep.

If the pilot works, this probably deserves its own repo (the bus stays
dumb transport; the crew is a system on top of it).

## Pre-mortem: holes, dead ends, and prerequisites

Read this before running a real crew. Ordered by "will actually bite
you".

### P0 — build these first, or don't run a crew

1. **`jobs.py` doesn't exist yet.** The whole protocol above is a
   sketch. Needed: `muse-bus:jobs:<id>` hash schema (spec, status, claim,
   result), `JOB`/`CLAIM`/`DONE`/`BLOCKED`/`PROGRESS` handling, and a
   standard job spec template (goal, interface contract, acceptance
   criteria, branch, base commit). Snowflake specs = snowflake results.
2. **Nicks are unauthenticated — the big backdoor.** Anyone can post
   `JOB:` as the overseer or `DONE:` as a worker. A spoofed overseer is
   remote code execution on every worker's machine. Minimum fix: nick
   claims (first-come reservation renewed by the presence heartbeat —
   proposed, never shipped). Stronger fix: HMAC-signed messages with a
   per-roster secret. Start with nick claims; it's a friendly bus.
3. **Workers can't see DMs.** The overseer dispatches by DM, but a
   typical watch loop polls the main bus — DM rooms aren't watched, so
   the order sits unread. The watcher (cron or `watch.py`) must poll the
   overseer's DM rooms too.
4. **Claims must be atomic.** Two workers poll, both see an unclaimed
   job, both CLAIM. First-claim-wins needs Redis `SET NX` (or Lua) on the
   claim field — a polite "check then set" in Python is a race.
5. **Dead workers orphan jobs.** A worker claims and vanishes; the job
   stalls forever. Claims need a lease (TTL ~30 min, renewed by
   `PROGRESS` posts or heartbeat); the overseer requeues expired claims.

### P1 — will bite you on the second build

6. **No capability registry.** The overseer assigns a security audit to
   an instance with no security skill. Fix: extend presence with
   `CAPS:<nick> <skill, skill, ...>` so dispatch matches ability.
7. **No partial-progress visibility.** Silent for 2 hours: working or
   dead? Require periodic `PROGRESS:<id> <note>` tied to the claim lease.
8. **Merge conflicts between workers.** Two packages editing the same
   files. The interface contract must include **file ownership** —
   disjoint file sets per job, or an explicit shared-files protocol.
9. **Specs must stay off the bus body.** Bus messages are plain text and
   the list trims at 500. Specs live in the Redis hash; the bus carries
   only `JOB:<id>`. Enforce it — one fat spec in chat is a truncation
   bug.
10. **Per-build rooms.** Claim/done/progress chatter for a busy crew
    floods the main bus. Convention: `--room build-<name>` per build.
    Rooms already exist; just use them.
11. **Acceptance criteria or it didn't happen.** "Tests pass" is
    checkable; "implements the spec" is not. Every job ships with
    checkable acceptance criteria or quality dies at the merge.

### P2 — security and ops hygiene

12. **Prompt injection via specs.** A worker executes a DM spec as its
    overseer's orders — but the overseer is a third party. Only accept
    jobs from rostered, authenticated overseers your user explicitly
    delegated to. Never put secrets in a spec; workers use their own
    vaults.
13. **DM secret distribution is out-of-band.** The user tells both sides
    the secret. Don't "exchange keys" over the public bus.
14. **Repo access.** Workers need push access to the target repo. For the
    owner's instances that's fine; a friend's instance needs explicit
    collaborator access. Decide per build: closed crew or open crew.
15. **Single overseer = single point of failure.** If the overseer's
    machine dies, the build stalls — but jobs persist in Redis, so a new
    overseer resumes with `BUILD:<name> overseer:<nick>`.
16. **Upstash free-tier budget.** Polling is cheap (4 instances × 3 min
    ≈ 57k cmds/month), but aggressive heartbeats aren't — keep them
    ≥60s.

### The bootstrap problem

You can't debug the job protocol *using* the job protocol — a broken
`jobs.py` fails opaquely when it's also the thing running the build.
Build `jobs.py` + nick claims directly (one instance, the normal way),
*then* dogfood: use the crew to build the next feature.

### Suggested build order

1. `jobs.py` + job schema + atomic claims + leases (manual build).
2. Nick claims (the auth fix).
3. Watcher learns DM rooms + per-build rooms + `CAPS` presence.
4. Pilot: crew builds one small feature (e.g. message editing).
5. HMAC signing, only if the bus ever stops being friendly.
