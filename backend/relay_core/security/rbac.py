"""Workspace RBAC (docs/system-design.md section 18.2).

Every row of the section 18.2 permissions matrix reads as "this role and every
role above it" (e.g. "Delete workspace" is owner-only, "Manage connectors & tools"
is admin-and-owner), so a single rank order is enough to encode the whole table —
no per-action exception list is needed.
"""

from enum import IntEnum


class Role(IntEnum):
    viewer = 0
    member = 1
    admin = 2
    owner = 3


def role_rank(role: str) -> int:
    return Role[role].value


def has_at_least(role: str, minimum: str) -> bool:
    return role_rank(role) >= role_rank(minimum)
