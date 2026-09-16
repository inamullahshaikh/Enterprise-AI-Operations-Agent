import pytest

from relay_core.security.rbac import Role, has_at_least, role_rank

ROLES_LOW_TO_HIGH = ["viewer", "member", "admin", "owner"]


def test_role_rank_orders_correctly() -> None:
    ranks = [role_rank(r) for r in ROLES_LOW_TO_HIGH]
    assert ranks == sorted(ranks)


@pytest.mark.parametrize(
    ("role", "minimum", "expected"),
    [
        ("owner", "owner", True),
        ("owner", "viewer", True),
        ("admin", "owner", False),
        ("member", "admin", False),
        ("viewer", "member", False),
        ("member", "member", True),
    ],
)
def test_has_at_least(role: str, minimum: str, expected: bool) -> None:
    assert has_at_least(role, minimum) is expected


def test_unknown_role_raises() -> None:
    with pytest.raises(KeyError):
        role_rank("superadmin")


def test_role_enum_matches_db_check_constraint_values() -> None:
    # docs/system-design.md section 14.3: workspace_members.role CHECK constraint.
    assert {r.name for r in Role} == {"owner", "admin", "member", "viewer"}
