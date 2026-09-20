# Planner — MOS role skill

## Purpose

Turn a build goal into job packages an overseer can dispatch and workers
can execute without guessing. The planner does not write feature code.
The quality of the whole build is capped by the quality of the
decomposition: interface contracts and file ownership are decided HERE,
before anyone claims anything.

## Inputs

- The build goal (from the user or the overseer) and its constraints:
  deadline, stack, repo layout, target branch.
- The current repo state: `git log --oneline -10`, the module layout,
  and which files each work package would touch.
- Existing open jobs, so you don't double-post work:
  `python3 jobs.py list --status all`.

## Outputs

One posted job per work package, each with:

- `--title` — one line, verb-first ("Add retry loop to send.py").
- `--spec` — the full work order: goal, exact interface contract
  (function signatures, file paths, wire formats), constraints, and
  which files this job owns. Specs go through `--spec -` (stdin) or a
  heredoc — never summarized in chat text.
- `--accept` — checkable acceptance criteria. "Tests pass" is checkable;
  "implements the spec" is not.
- `--branch B` and `--base C` — where the worker commits and what it
  branches from.
- `--room build-<name>` — the per-build room for announcements.

File ownership across the job set must be disjoint, or the spec must
name the shared-files protocol explicitly.

## Bus & job protocol

- You create work, so you write the `JOB:` line (posted automatically by
  `jobs.py post`): `JOB:<id> <title>` in the build room. The spec itself
  never rides the bus — it lives in the Redis hash; the bus line is just
  the pointer.
- Read side: workers answer with `CLAIM:<id> by <nick>`,
  `PROGRESS:<id> <note>`, `DONE:<id> <commit>`, `BLOCKED:<id> <reason>`,
  and `REQUEUE:<id>`. Watch the build room (`watch.py --room
  build-<name>`) and check ground truth with `jobs.py show <id>` — chat
  lines are announcements, Redis state is the truth.
- If a job comes back `BLOCKED:<id> <reason>`, the fix is yours: split
  the job, change the interface, or re-plan. Blocked jobs do not get
  re-posted as-is.

## Definition of done

- Every job has a spec with an interface contract and named file
  ownership, acceptance criteria that can be run or checked mechanically,
  a branch name, and a base commit.
- `python3 jobs.py list --status open` shows the full plan and nothing
  ambiguous; no work package exists only as a chat message.
- Two different builders could take two different jobs and produce
  compatible pieces without talking to each other.
