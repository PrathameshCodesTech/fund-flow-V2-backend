from apps.core.models import ScopeNode


def build_node_path(parent, org, code):
    """Build materialized path for a ScopeNode."""
    if parent:
        return f"{parent.path}/{code}"
    return f"/{org.code}/{code}"


def get_node_depth(parent):
    """Depth is 0 for direct children of org."""
    if parent:
        return parent.depth + 1
    return 0


def update_descendant_paths(node, old_path):
    """
    When a node's path changes (code or parent changed),
    update all descendant paths and depths in bulk.
    """
    descendants = ScopeNode.objects.filter(
        org=node.org,
        path__startswith=old_path + "/",
    )
    updates = []
    for desc in descendants:
        new_path = node.path + desc.path[len(old_path):]
        desc.path = new_path
        desc.depth = new_path.strip("/").count("/")
        updates.append(desc)
    if updates:
        ScopeNode.objects.bulk_update(updates, ["path", "depth"])


def get_subtree_nodes(node):
    """All descendants including the node itself."""
    return ScopeNode.objects.filter(
        org=node.org,
        path__startswith=node.path,
    ).order_by("depth", "name")


def get_ancestors(node):
    """All ancestors ordered root-first."""
    ancestor_paths = []
    parts = node.path.strip("/").split("/")
    for i in range(1, len(parts)):
        ancestor_paths.append("/" + "/".join(parts[:i]))
    if not ancestor_paths:
        return ScopeNode.objects.none()
    return ScopeNode.objects.filter(
        org=node.org,
        path__in=ancestor_paths,
    ).order_by("depth")
