"""MOS — multi-agent orchestration layer above the muse-relay bus.

The bus stays transport; this package is the orchestration layer:

  capabilities  who can do what (heartbeated registry, Redis-backed)
  acceptance    machine-readable acceptance criteria + a verifier
  overseer      dispatch work via DMs, verify results, requeue failures
  worker        poll DMs, claim matching jobs, heartbeat, done/blocked
  reconcile     restart/failover recovery (idempotent)
  gitops        safe Git review/merge — workers never push to main

Wire messages (JOB:/CLAIM:/...) are announcements; Redis is truth.
"""
