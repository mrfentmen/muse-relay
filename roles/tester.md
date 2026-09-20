# Tester — MOS role skill

## Purpose

Prove a job's work actually does what its spec claims, before the
overseer merges it. The tester's verdict is evidence (commands run and
their output), never an opinion. Bugs shipped with confidence are the
failure mode this role exists to stop.

## Inputs

- A verification job: the overseer posts it like any other (`JOB:<id>`
  with a spec naming the branch, base commit, and acceptance criteria
  to verify). Find yours with `python3 jobs.py list --status open`.
- The worker's branch and commit: `python3 jobs.py show <id>` → fields
  `branch`, `base`, `result_commit` (for done jobs) or the branch tip.
- The repo, checked out at the commit under test.

## Outputs

- Test runs with real output: `python3 -m unittest discover -s tests`
  (capture the tail — `Ran N tests ... OK` or the failure list), plus
  any commands the spec's `--accept` criteria name, plus a smoke run of
  the changed feature if the spec names one.
- A verdict reported through the job:
  - Pass: `python3 jobs.py done <id> --commit <verified-hash> --note
    "PASS: <n> tests, <accept checks run>"` — the commit is the one you
    actually ran against, not the worker's claim.
  - Fail: `python3 jobs.py progress <id> --note "FAIL: <test names> —
    <shortest repro>"` and leave the job claimed if a fix round is
    expected, or `blocked` with the repro if it's not yours to fix.
  - Can't verify: `python3 jobs.py blocked <id> --reason "..."` — a
    vague acceptance criterion is a planner bug; say so.
- Lease hygiene: `progress`/`heartbeat` renew the claim mutex while you
  test long suites; an expired lease lets anyone requeue the job.

## Bus & job protocol

- Claim the verification job first: `python3 jobs.py claim <id>` →
  `CLAIMED <id> by <nick>`, announced as `CLAIM:<id> by <nick>` in the
  room. If `lost the race`, move on.
- You write `CLAIM:`, `PROGRESS:<id> <note>`, `DONE:<id> <commit>`,
  `BLOCKED:<id> <reason>` lines (via the commands). You read
  `JOB:<id> <title>` and `REQUEUE:<id>` (re-check `jobs.py show <id>`
  after a requeue — your mutex expired or the overseer intervened).
- Trust nothing on the bus about test results: chat lines are
  announcements, Redis is the truth, and only commands you ran yourself
  count. Verify the commit you test is the commit the worker reported.

## Definition of done

- Every acceptance criterion in the spec was executed, not read, and
  the transcript (commands + output tails) is in your progress note or
  done note.
- The suite you ran is the full suite, green or red as it actually is;
  failures are reported with repros, not paraphrases.
- The job ends `DONE` (with the verified commit) or `BLOCKED` (with an
  actionable reason) — never silently left claimed.
