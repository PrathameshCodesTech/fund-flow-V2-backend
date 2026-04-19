from django.conf import settings
from django.db import models


class Role(models.Model):
    """
    Role scoped to an Organization. code is unique per org.
    node_type_scope optionally restricts which node types this role applies to.
    """
    org = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="roles",
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=100, help_text="Unique within org")
    node_type_scope = models.CharField(
        max_length=50,
        blank=True,
        help_text="Which node_type this role is valid for. Empty = all types.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "roles"
        constraints = [
            models.UniqueConstraint(fields=["org", "code"], name="unique_role_code_per_org"),
        ]

    def __str__(self):
        return f"{self.org.code}:{self.code}"


class PermissionAction(models.TextChoices):
    CREATE = "create", "Create"
    READ = "read", "Read"
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"
    APPROVE = "approve", "Approve"
    REJECT = "reject", "Reject"
    REASSIGN = "reassign", "Reassign"
    START_WORKFLOW = "start_workflow", "Start Workflow"
    MANAGE_MODULE = "manage_module", "Manage Module"


class PermissionResource(models.TextChoices):
    INVOICE = "invoice", "Invoice"
    CAMPAIGN = "campaign", "Campaign"
    VENDOR = "vendor", "Vendor"
    BUDGET = "budget", "Budget"
    WORKFLOW = "workflow", "Workflow"
    MODULE = "module", "Module"
    USER = "user", "User"
    ROLE = "role", "Role"


class Permission(models.Model):
    """
    System-wide permission. Combination of action + resource is globally unique.
    Permissions are org-agnostic; RolePermission links them to roles.
    """
    action = models.CharField(max_length=50, choices=PermissionAction.choices)
    resource = models.CharField(max_length=50, choices=PermissionResource.choices)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "permissions"
        constraints = [
            models.UniqueConstraint(fields=["action", "resource"], name="unique_permission"),
        ]

    def __str__(self):
        return f"{self.action}:{self.resource}"


class RolePermission(models.Model):
    """Junction: which permissions a role has."""
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="role_permissions")

    class Meta:
        db_table = "role_permissions"
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="unique_role_permission"),
        ]

    def __str__(self):
        return f"{self.role} → {self.permission}"


class AssignmentType(models.TextChoices):
    PRIMARY = "primary", "Primary"
    ADDITIONAL = "additional", "Additional"
    DELEGATED = "delegated", "Delegated"


class UserScopeAssignment(models.Model):
    """
    Tracks WHERE a user belongs in the hierarchy.
    Separate from authority — a user can belong to a node without having a role there.
    No downward inheritance: every row is explicit.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="scope_assignments",
    )
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.CASCADE,
        related_name="user_scope_assignments",
    )
    assignment_type = models.CharField(max_length=20, choices=AssignmentType.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_scope_assignments"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "scope_node", "assignment_type"],
                name="unique_user_scope_assignment",
            ),
        ]

    def __str__(self):
        return f"{self.user} @ {self.scope_node} [{self.assignment_type}]"


class UserRoleAssignment(models.Model):
    """
    Tracks WHAT a user can do at a specific node.
    Authoritative source for permission checks.
    No downward inheritance: every row is explicit.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="user_assignments",
    )
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.CASCADE,
        related_name="user_role_assignments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_role_assignments"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role", "scope_node"],
                name="unique_user_role_assignment",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "scope_node"]),
            models.Index(fields=["role", "scope_node"]),
        ]

    def __str__(self):
        return f"{self.user} → {self.role} @ {self.scope_node}"
