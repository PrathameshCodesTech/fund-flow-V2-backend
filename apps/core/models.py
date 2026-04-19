from django.db import models


class Organization(models.Model):
    """
    Tenant root. Fixed, never generic. Every ScopeNode points back here.
    """
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50, unique=True, help_text="Stable slug used in materialized paths")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organizations"

    def __str__(self):
        return f"{self.name} ({self.code})"


class NodeType(models.TextChoices):
    COMPANY = "company", "Company"
    ENTITY = "entity", "Entity"
    REGION = "region", "Region"
    BRANCH = "branch", "Branch"
    DEPARTMENT = "department", "Department"
    COST_CENTER = "cost_center", "Cost Center"


class ScopeNode(models.Model):
    """
    Generic hierarchy node below Organization.
    Supports any depth via self-referential parent.

    Materialized path strategy:
        path  = /org_code/company_code/entity_code
        depth = 0 for direct children of org

    Sibling uniqueness is enforced on (org, parent, code).
    'code' is preferred over 'name' for uniqueness — stable, URL-safe slug.
    """
    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="scope_nodes",
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=100, help_text="Stable slug, unique among siblings")
    node_type = models.CharField(max_length=50, choices=NodeType.choices)
    path = models.CharField(
        max_length=2000,
        help_text="Materialized path e.g. /org_code/company_code/entity_code",
    )
    depth = models.PositiveIntegerField(
        default=0,
        help_text="0 = direct child of org",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scope_nodes"
        constraints = [
            models.UniqueConstraint(
                fields=["org", "parent", "code"],
                name="unique_sibling_code",
            ),
        ]
        indexes = [
            models.Index(fields=["path"]),
            models.Index(fields=["org", "node_type"]),
            models.Index(fields=["org", "is_active"]),
        ]

    def __str__(self):
        return f"{self.node_type}:{self.code} ({self.org.code})"

    def get_ancestors_from_path(self):
        """
        Returns path segments that represent ancestor paths.
        Used for ancestor lookups without recursive queries.
        """
        parts = self.path.strip("/").split("/")
        ancestors = []
        for i in range(1, len(parts)):
            ancestors.append("/" + "/".join(parts[:i]))
        return ancestors
