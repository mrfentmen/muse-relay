#!/usr/bin/env python3
"""Role skills: the personas a crew member can work as.

roles/<name>.md holds a short persona brief (planner, builder, tester,
reviewer, documenter, overseer). They are wired in two places:

  - a job can require a role (jobs.py post --role reviewer), so the
    overseer dispatches it to the crew member whose brief fits it
    (see mos/capabilities.py find_for),
  - a worker loads its own brief and hands it to whatever runs the job
    (mos.py role builder), so "roles as skills" reaches the thing that
    actually does the work instead of being a string on a registration
    record that nobody reads.

Roles load from this repo's roles/ directory only, and names are checked
against [a-z0-9_-]+ — so a name can never walk out of the directory.
"""
import os
import re

ROLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roles")

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class RoleError(ValueError):
    """Raised for a malformed or unknown role name."""


def list_roles():
    """Names of the role skills in roles/, sorted."""
    try:
        entries = os.listdir(ROLES_DIR)
    except OSError:
        return []
    return sorted(n[:-3] for n in entries
                  if n.endswith(".md") and _NAME.match(n[:-3]))


def role_path(name):
    """Path to a role's brief. Raises RoleError for bad/unknown names."""
    clean = (name or "").strip().lower()
    if not _NAME.match(clean):
        raise RoleError(f"invalid role name: {name!r} (want [a-z0-9_-]+)")
    path = os.path.join(ROLES_DIR, clean + ".md")
    if not os.path.isfile(path):
        have = ", ".join(list_roles()) or "none"
        raise RoleError(f"unknown role {clean!r} (available: {have})")
    return path


def load_role(name):
    """Return a role's brief text. Raises RoleError when unknown."""
    with open(role_path(name), encoding="utf-8") as f:
        return f.read()


def parse_roles(value):
    """'builder, tester' -> ['builder', 'tester'] (validated, deduped).

    Raises RoleError on an unknown or malformed name, so a typo at
    registration or dispatch time fails loudly instead of registering a
    role that matches nobody forever.
    """
    out = []
    for raw in (value or "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        role_path(name)  # validates both the shape and that it exists
        if name not in out:
            out.append(name)
    return out
