"""Branch visibility helpers for AI tools."""
from __future__ import annotations


def employee_branch_scope(company_id: int) -> tuple[bool, int | None]:
    """Return (restricted, branch_id) for an authenticated tenant employee.

    Company administrators and unauthenticated channel agents retain company-wide
    scope. Employees are limited to their currently selected, assigned branch.
    A missing/mismatched employee context resolves to an impossible branch ID.
    """
    from flask_login import current_user

    if not getattr(current_user, "is_authenticated", False):
        return False, None

    role = str(getattr(current_user, "role", "") or "").strip().lower()
    if role in {"admin", "superadmin"}:
        return False, None

    try:
        expected_company_id = int(company_id)
        user_company_id = int(getattr(current_user, "company_id", 0) or 0)
    except (TypeError, ValueError):
        return True, -1
    if user_company_id != expected_company_id:
        return True, -1

    from stockarmobile.branch_context import get_active_branch_id
    branch_id = get_active_branch_id()
    return True, int(branch_id) if branch_id is not None else -1
