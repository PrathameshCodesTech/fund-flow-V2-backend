from django.db import models


class ModuleType(models.TextChoices):
    INVOICE = "invoice", "Invoice"
    CAMPAIGN = "campaign", "Campaign"
    VENDOR = "vendor", "Vendor"
    BUDGET = "budget", "Budget"


class ModuleActivation(models.Model):
    """
    Controls whether a module is active at a given ScopeNode.

    Resolution contract (walk-up resolver):
        1. Start at subject's node
        2. Has a row for this module?
               YES + override_parent=True  → use is_active (stop)
               YES + override_parent=False → ignore, walk to parent
               NO                          → walk to parent
        3. First explicit decision wins. No decision → default OFF.

    override_parent=False (default) means this row is informational only
    unless no ancestor overrides it.
    """
    module = models.CharField(max_length=50, choices=ModuleType.choices)
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.CASCADE,
        related_name="module_activations",
    )
    is_active = models.BooleanField(default=False)
    override_parent = models.BooleanField(
        default=False,
        help_text="If True, this row's is_active is authoritative regardless of parent.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "module_activations"
        constraints = [
            models.UniqueConstraint(
                fields=["module", "scope_node"],
                name="unique_module_per_node",
            ),
        ]
        indexes = [
            models.Index(fields=["scope_node", "module"]),
        ]

    def __str__(self):
        state = "ON" if self.is_active else "OFF"
        override = " [override]" if self.override_parent else ""
        return f"{self.module} @ {self.scope_node} → {state}{override}"
