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

### How it would work

1. **Decompose.** The overseer breaks the build into work packages with
   clean interfaces (module boundaries, APIs, file ownership) *before*
   anyone writes code.
2. **Dispatch.** Work packages go on the bus as jobs (see protocol
   below). Workers claim them.
3. **Build.** Each worker pulls the repo, branches, builds its package
   against the interface contract, tests it, pushes.
4. **Verify.** The overseer pulls each branch, runs the tests, checks the
   contract — rejects or accepts.
5. **Merge.** Overseer merges accepted branches, runs the full suite,
   ships.

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

1. Two workers + overseer, one small build (e.g. a single-page site).
2. Overseer writes the interface contract up front, posts 2–3 jobs.
3. Run it, note every place it snags (it will snag).
4. Only then decide whether the 4-role assembly line earns its keep.

If the pilot works, this probably deserves its own repo (the bus stays
dumb transport; the crew is a system on top of it).
