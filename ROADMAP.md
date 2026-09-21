# Roadmap & the crew idea

Ideas captured 2026-09-20. Re-checked against the code 2026-09-21: the
status notes below are what is actually in the repo, not what was
planned. Everything marked SHIPPED is implemented and tested.

**Where the crew stands today.** The whole pre-mortem P0 list is
*shipped* (it was written before any of it existed, and it read as an
open to-do list for a while — that was misleading). The crew can run
without a human in the loop: workers poll, claim, work and report on
their own, with leases renewed during the work, open-job fallback when a
dispatch is missed, and file-ownership enforced at claim time.

The 2026-09-21 guardrail pass closed the failure modes that made
unattended operation lie about itself: a job now has an attempt budget
and is dead-lettered instead of retried forever, a worker cannot claim
more than it can work on, a job can declare dependencies and is not
claimable until they are done, acceptance can fail a branch that wrote
outside its declared files, the overseer alerts on jobs nobody is coming
for, and `mos.py status`/`mos.py doctor` make the crew and its setup
inspectable. What is still genuinely open is at the bottom, and it is
much shorter than it used to be.

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

### Roles as skills — SHIPPED

`roles/*.md` holds the persona briefs (`planner`, `builder`, `tester`,
`reviewer`, `documenter`, `overseer`), and they are wired to real
behavior rather than being a string on a registration record:

- a job can require a role (`jobs.py post --role`, `mos.py dispatch
  --role`), only workers registered with it get the DM, and a worker
  holding none of the requested roles skips the job;
- a worker loads its own brief and can hand it to whatever runs the job
  (`mos.py roles builder`), which is how the persona actually reaches the
  model doing the work.

An unknown role name is rejected at registration and dispatch time, so a
typo cannot register a crew member nobody will ever pick. The suggested
starter set (`architect`/`frontend`/`backend`/`security`/`qa`) is not
shipped — the shipped names are the six above; add more by dropping a
`.md` file in `roles/`.

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

### P0 — build these first, or don't run a crew — ALL SHIPPED

1. **`jobs.py` exists.** `muse-bus:job:<id>` hashes
   (title/spec/status/claim/result/accept), `JOB`/`CLAIM`/`DONE`/
   `BLOCKED`/`PROGRESS`/`REQUEUE` announcements, and machine-readable
   acceptance criteria on every job (`--accept`, checked by the
   overseer with `mos.py tick`). SHIPPED.
2. **Nicks are still unauthenticated, but the crew has a floor under
   it.** Nick claims shipped (`muse-bus:nickclaim:<nick>`, SET NX, renewed
   by every presence heartbeat) — a tripwire, not auth. The real fix for
   job spoofing also shipped: HMAC-signed jobs (v4 — keyring with key
   rotation, expiry, nonce, bound to the exact job id, covering the
   ownership declaration and the deps/attempt-budget controls) and a
   worker that
   refuses a BAD/EXPIRED signature and posts a loud `REJECTED:` alert.
   Signing is opt-in per crew; an unsigned job is still claimable, so
   use `--secret-file` when the crew is not fully trusted. SHIPPED.
3. **Workers see DMs.** The MOS worker polls its DM dead-drop with a
   Redis-side offset; DMs were made repeatable (`--dm` repeatable on
   `poll.py`/`watch.py`) and per-build rooms are a documented convention.
   SHIPPED.
4. **Claims are atomic.** `SET NX EX` on `muse-bus:job:<id>:claim`, with
   a rollback if the claim record fails to write. SHIPPED.
5. **Dead workers don't orphan jobs.** Claims are leases; the worker
   renews them (`heartbeat`/`progress`, and automatically while a handler
   runs), and `sweep`/`tick`/the next `claim` requeue an expired one.
   SHIPPED.

### P1 — will bite you on the second build

6. **No capability registry.** The overseer assigns a security audit to
   an instance with no security skill. Fix: extend presence with
   `CAPS:<nick> <skill, skill, ...>` so dispatch matches ability.
   SHIPPED (`mos/capabilities.py`: `mos.py register --caps/--roles/
   --max-jobs`, matched by dispatch and by a worker's own scan).
7. **No partial-progress visibility.** Silent for 2 hours: working or
   dead? Require periodic `PROGRESS:<id> <note>` tied to the claim lease.
   SHIPPED (`jobs.py progress` renews the lease, and `mos.py tick`
   announces a job that sits open past `MUSE_RELAY_STUCK_AFTER` with no
   claim — including when no capable worker exists at all).
8. **Merge conflicts between workers.** Jobs declare `--files a.py,b.py`
   and the claim is refused while a live job owns any of them
   (`--force` overrides). This is a guard, not a substitute for the
   interface contract — the overseer still has to hand out disjoint
   sets. Guard SHIPPED; the contract discipline is still on the
   overseer.
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
    SHIPPED as a mechanism (`--accept` with `file-exists`/`tests-pass`/
    `command-ok`/`contains`/`files-only`, run by `mos.py tick`); the
    discipline of actually posting criteria is still on the overseer, and
    `mos.py dispatch` requires `--files` plus `--base` to add the
    `files-only` check itself.

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

### Suggested build order — done, and what replaced it

1. ~~`jobs.py` + job schema + atomic claims + leases~~ SHIPPED.
2. ~~Nick claims~~ SHIPPED.
3. ~~Watcher learns DM rooms + per-build rooms + `CAPS` presence~~
   SHIPPED (capability registry in `mos/capabilities.py`).
4. ~~Pilot: crew builds one small feature~~ SHIPPED (message editing is
   in, and the crew built on top of it).
5. ~~HMAC signing, only if the bus ever stops being friendly~~ SHIPPED
   (v3 signatures, keyring rotation, expiry).
6. **Unattended operation** — SHIPPED 2026-09-21: `mos.py worker --loop
   --handler CMD` runs the poll/claim/work/report loop with nobody
   driving it, renews its own lease while a handler runs, and falls back
   to scanning open jobs when a dispatch was missed. `mos.py tick --loop`
   does the same for the overseer side (sweep, prune, verify).
7. **Flow guardrails** — SHIPPED 2026-09-21: attempt budgets with
   dead-lettering (`--max-attempts`, `jobs.py reset`), dependency-gated
   claims (`--depends-on`), the `files-only` acceptance check (added
   automatically by dispatch when files and a base are given), a local
   claim budget in the worker (`--max-jobs`), stuck alerts in `tick`, a
   secret tripwire on specs, and `mos.py status`/`mos.py doctor`.

### What is genuinely still open

The honest list, after the 2026-09-21 pass. These are not blockers for a
friendly crew; they are the things that will bite when the crew gets
bigger or less trusted.

1. **The overseer's decompose step is still manual.** `--files` is
   enforced, but nothing writes the interface contract or splits a build
   into work packages — a human (or the overseer agent's own judgment)
   does that. This is the job the pre-mortem called "senior-architect
   work", and it is still the hard part.
2. **The handler contract is unverified beyond its exit code.** A
   handler can print a plausible commit hash without doing the work. The
   acceptance check in `mos.py tick` is the actual gate — run it.
3. **Multiple overseers.** Two overseers can dispatch overlapping work;
   the file guard catches collisions at claim time, but nothing elects a
   single dispatcher.
4. **Bus content is still untrusted.** Signed jobs cover the queue; a
   spec's *text* is still an instruction from a third party. `INSTANCES.md`
   is explicit that only your own user's orders are real instructions.
5. **The web viewer's cursor is trim-aware now, but only as good as the
   counter.** `bus.html` reads `llen` + `<key>:total` like
   `relay_common.bus_read` and warns when it was outrun
   (`tests/test_bus_html_cursor.py` runs the real `readWindow()` under
   node). Lists written before the counter existed fall back to plain
   index behavior for that tab (no warning, no loss detection) until
   they are pushed to again — the counter only appears once something
   else has pushed.
6. **A worker still runs one job at a time.** `--max-jobs N` bounds how
   many jobs a worker takes per cycle, but it works them sequentially,
   because every handler shares one workdir — two handlers in one
   checkout would fight over the same files. Real concurrency needs a
   git worktree per job (the overseer already does this for verification)
   and a way to merge them back, which is not built yet.
7. **A dead-lettered job still needs a human.** `sweep`/`tick` stop the
   retry loop and announce it, but nothing re-plans the work, weakens the
   acceptance criteria, or reassigns it to a different role. That is the
   overseer agent's judgment call, by design.
