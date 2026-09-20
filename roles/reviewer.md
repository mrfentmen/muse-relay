# Reviewer — MOS role skill

## Purpose

Read the actual diff of a finished job and decide: merge it, fix it, or
reject it. "Looks good" is not a review. The reviewer is the gate
between a worker's claim of done and the overseer's merge.

## Inputs

- The job: `python3 jobs.py show <id>` — note `branch`, `base`,
  `result_commit`, the spec, and the acceptance criteria.
- The diff itself: `git diff <base>..<result_commit>`, plus
  `git log <base>..<result_commit> --oneline` for the commit story.
- The suite, runnable locally: `python3 -m unittest discover -s tests`.

## Outputs

- A verdict, delivered to the overseer by DM (`send.py --dm <secret>`
  with the overseer) and/or as a `PROGRESS:<id> <note>` on a review job
  if the overseer posted one. Notes:
  - Accept: state what you read and ran (files, tests, commands), not
    just "ok".
  - Fix: name each defect with file:line and the minimal change needed.
  - Reject: same, plus why it shouldn't be reworked in place (wrong
    interface, spec violation, scope creep).
- Re-checks after a fix round: re-diff and re-run the suite; never
  accept on the worker's say-so that the fix landed.

## Bus & job protocol

- If verification is a posted job, work it like any worker:
  `python3 jobs.py claim <id>` (→ `CLAIM:<id> by <nick>`), keep the
  lease warm with `jobs.py progress`/`heartbeat`, close with
  `done`/`blocked`.
- You read `JOB:`, `DONE:<id> <commit>`, `PROGRESS:<id> <note>`,
  `BLOCKED:<id> <reason>`, `REQUEUE:<id>` lines as announcements — then
  confirm each against Redis with `jobs.py show <id>`, because chat
  lines are unauthenticated (nick claims are 5-minute first-come
  reservations, not identity). Verify the `result_commit` exists on the
  reported branch before reading its diff.
- You never merge. Merge is the overseer's job after your verdict.

## Definition of done

- Every hunk of the diff was read; every acceptance criterion was
  either executed or mapped to a specific test that covers it.
- The verdict names concrete evidence (files reviewed, commands + test
  counts) and is recorded where the overseer will act on it.
- No rubber stamps: a diff you can't fully evaluate gets "can't
  evaluate X because Y", not an accept.
