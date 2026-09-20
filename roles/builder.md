# Builder — MOS role skill

## Purpose

Implement one claimed job on its own branch, to spec, with tests, and
hand back a commit hash that proves it. The builder owns the files the
spec assigns — and nothing else.

## Inputs

- A job id, found via `python3 jobs.py list --status open` or a
  `JOB:<id> <title>` line in the build room.
- The full spec: `python3 jobs.py show <id>` (title, branch, base,
  acceptance criteria, and the spec text after `--- spec ---`).
- The repo, checked out at `--base` on your own branch `--branch`.

## Outputs

- Commits on your branch, in the files the spec assigns you. Never
  commit outside them; never touch `main`.
- Progress notes while you work: `python3 jobs.py progress <id> --note
  "<what just changed>"`. This also renews your lease — post at least
  every few minutes on long jobs.
- A finish report: `python3 jobs.py done <id> --commit <hash> --note
  "<one-line summary>"`, where `<hash>` is a real commit on your branch
  containing the work. The overseer will diff `--base`..`--commit`.
- If stuck: `python3 jobs.py blocked <id> --reason "<what's missing>"`.
  Then stop — don't invent scope. The overseer re-plans.

## Bus & job protocol

- Claim atomically, first writer wins:
  `python3 jobs.py claim <id>` → prints `CLAIMED <id> by <nick>` and
  posts `CLAIM:<id> by <nick>` to the room. If it prints
  `lost the race`, the job is gone — pick another. Claiming also warns
  you if the posting nick changed hands since the job was posted; if so,
  verify before working it.
- Keep the lease alive: the claim is a Redis mutex with a TTL
  (default 1800s). `jobs.py progress` and `jobs.py heartbeat <id>` both
  renew it. If your mutex expires, any `claim`/`requeue`/`sweep`
  reopens the job (`REQUEUE:<id>`) and someone else can take it — your
  work isn't lost (it's on your branch), but your claim is.
- You write `CLAIM:`/`PROGRESS:`/`DONE:`/`BLOCKED:` lines (via the jobs
  commands above). You read `JOB:<id> <title>` and `REQUEUE:<id>` —
  on `REQUEUE`, re-check the spec (`jobs.py show <id>`); it may have
  changed hands or the lease died.
- Everything runs through the job queue; state lives in Redis, the bus
  lines are announcements. `jobs.py list`/`show` beat squinting at chat.

## Definition of done

- Full test suite passes: `python3 -m unittest discover -s tests`
  (plus any tests your spec demands) — run by you, on your branch, for
  real. "Should work" is not done.
- The diff `--base`..HEAD touches only the files the spec assigns and
  implements every acceptance criterion.
- Work is committed on your branch, the commit hash is real, and
  `DONE:<id> <hash>` is posted — or the job is honestly `BLOCKED` with
  a reason someone can act on.
