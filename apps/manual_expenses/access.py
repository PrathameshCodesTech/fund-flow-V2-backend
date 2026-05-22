"""
Manual expense access helpers.

Manual expenses intentionally use inherited scope authority for mutations:
if a user is assigned at a parent scope, they may act on expense records in
child scopes within that subtree.

This is a module-specific exception. The global access model remains:
  - visibility: subtree
  - authority: exact node only
"""

from apps.access.selectors import get_user_visible_scope_ids


def get_user_manual_expense_scope_ids(user) -> list[int]:
    """
    Return the scope ids where the user may act on manual expenses.

    Manual expenses follow subtree inheritance for actions, so we reuse the
    user's visible scopes rather than the platform's exact-node actionable
    scopes.
    """
    return get_user_visible_scope_ids(user)
