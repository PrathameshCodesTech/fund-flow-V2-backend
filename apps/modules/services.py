from apps.modules.models import ModuleActivation
from apps.core.services import get_ancestors


def resolve_module_activation(module, scope_node):
    """
    Walk-up resolver for module activation.

    Contract (from PLATFORM_ARCHITECTURE.md):
        1. Start at subject's scope_node
        2. Has a ModuleActivation row for this module?
               YES + override_parent=True  → use is_active (authoritative, stop)
               YES + override_parent=False → skip this row, continue walking up
               NO                          → continue walking up
        3. First row with override_parent=True wins.
        4. No such row found → default OFF (return False).

    Returns: bool — whether the module is active for this node.
    """
    nodes_to_check = [scope_node] + list(get_ancestors(scope_node).order_by("-depth"))
    for node in nodes_to_check:
        try:
            activation = ModuleActivation.objects.get(module=module, scope_node=node)
            if activation.override_parent:
                return activation.is_active
            # override_parent=False: informational only, keep walking
        except ModuleActivation.DoesNotExist:
            pass
    return False


def set_module_activation(module, scope_node, is_active, override_parent=False):
    """Create or update a ModuleActivation row."""
    obj, _ = ModuleActivation.objects.update_or_create(
        module=module,
        scope_node=scope_node,
        defaults={"is_active": is_active, "override_parent": override_parent},
    )
    return obj
