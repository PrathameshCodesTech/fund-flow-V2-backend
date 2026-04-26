from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class VersionStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    ARCHIVED = "archived", "Archived"


class ParallelMode(models.TextChoices):
    SINGLE = "SINGLE", "Single"
    ALL_MUST_APPROVE = "ALL_MUST_APPROVE", "All Must Approve"
    ANY_ONE_APPROVES = "ANY_ONE_APPROVES", "Any One Approves"


class RejectionAction(models.TextChoices):
    TERMINATE = "TERMINATE", "Terminate"
    GO_TO_GROUP = "GO_TO_GROUP", "Go To Group"
    PREVIOUS_STAGE = "PREVIOUS_STAGE", "Previous Stage"
    RETURN_TO_SPLITTER = "RETURN_TO_SPLITTER", "Return To Splitter"
    RETURN_TO_SUBMITTER = "RETURN_TO_SUBMITTER", "Return To Submitter"
    BRANCH_CORRECTION = "BRANCH_CORRECTION", "Branch Correction"


class AssignmentMode(models.TextChoices):
    ROLE_RESOLVED = "ROLE_RESOLVED", "Role Resolved"
    EXPLICIT_USER = "EXPLICIT_USER", "Explicit User"
    APPROVER_POOL = "APPROVER_POOL", "Approver Pool"
    RUNTIME_SELECTED_FROM_POOL = "RUNTIME_SELECTED_FROM_POOL", "Runtime Selected From Pool"


class AllocationTotalPolicy(models.TextChoices):
    MUST_EQUAL_INVOICE_TOTAL = "MUST_EQUAL_INVOICE_TOTAL", "Must Equal Invoice Total"
    CAN_BE_PARTIAL = "CAN_BE_PARTIAL", "Can Be Partial"


class ScopeResolutionPolicy(models.TextChoices):
    SUBJECT_NODE = "SUBJECT_NODE", "Subject Node"
    ANCESTOR_OF_TYPE = "ANCESTOR_OF_TYPE", "Ancestor Of Type"
    ORG_ROOT = "ORG_ROOT", "Org Root"
    FIXED_NODE = "FIXED_NODE", "Fixed Node"


class StepKind(models.TextChoices):
    """
    Controls how a step resolves its runtime behavior at activation time.

    NORMAL_APPROVAL        — standard role/scope resolution; one assignee per step
    SPLIT_BY_SCOPE         — fans out into one branch per resolved scope node;
                             parent instance pauses until all branches complete
    JOIN_BRANCHES          — special step that completes only after all branches
                             from the matching SPLIT_BY_SCOPE have resolved
    RUNTIME_SPLIT_ALLOCATION — splitter submits N allocation lines; each becomes a branch
    SINGLE_ALLOCATION      — splitter submits exactly one allocation line covering the
                             full invoice amount; no branch fanout, step auto-advances
    """
    NORMAL_APPROVAL = "NORMAL_APPROVAL", "Normal Approval"
    SPLIT_BY_SCOPE = "SPLIT_BY_SCOPE", "Split By Scope"
    JOIN_BRANCHES = "JOIN_BRANCHES", "Join Branches"
    RUNTIME_SPLIT_ALLOCATION = "RUNTIME_SPLIT_ALLOCATION", "Runtime Split Allocation"
    SINGLE_ALLOCATION = "SINGLE_ALLOCATION", "Single Allocation"


class BranchStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class JoinPolicy(models.TextChoices):
    ALL_BRANCHES_MUST_COMPLETE = "ALL_BRANCHES_MUST_COMPLETE", "All Branches Must Complete"


class BranchApprovalPolicy(models.TextChoices):
    """
    Controls whether runtime split allocation branches require explicit approval.

    REQUIRED_FOR_ALL  — every allocation line must have selected_approver;
                        each creates a PENDING branch; parent step waits for all.
    OPTIONAL_WHEN_CONFIGURED — only require approver when the split option has
                        approver_role or allowed_approvers configured. Otherwise
                        auto-approve. Mixed lines create a mix of PENDING and
                        APPROVED branches.
    SKIP_ALL — no approver required for any line; all branches are auto-approved
                        and the parent step completes immediately.
    """
    REQUIRED_FOR_ALL = "REQUIRED_FOR_ALL", "Required For All"
    OPTIONAL_WHEN_CONFIGURED = "OPTIONAL_WHEN_CONFIGURED", "Optional When Configured"
    SKIP_ALL = "SKIP_ALL", "Skip All"


class InstanceStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    FROZEN = "FROZEN", "Frozen"
    STUCK = "STUCK", "Stuck"


class GroupStatus(models.TextChoices):
    WAITING = "WAITING", "Waiting"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    RESET = "RESET", "Reset"


class StepStatus(models.TextChoices):
    WAITING = "WAITING", "Waiting"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    SKIPPED = "SKIPPED", "Skipped"
    ORPHANED = "ORPHANED", "Orphaned"
    REASSIGNED = "REASSIGNED", "Reassigned"
    WAITING_BRANCHES = "WAITING_BRANCHES", "Waiting Branches"


class WorkflowEventType(models.TextChoices):
    STEP_ASSIGNED = "STEP_ASSIGNED", "Step Assigned"
    STEP_APPROVED = "STEP_APPROVED", "Step Approved"
    STEP_REJECTED = "STEP_REJECTED", "Step Rejected"
    STEP_ORPHANED = "STEP_ORPHANED", "Step Orphaned"
    STEP_REASSIGNED = "STEP_REASSIGNED", "Step Reassigned"
    INSTANCE_STUCK = "INSTANCE_STUCK", "Instance Stuck"
    INSTANCE_FROZEN = "INSTANCE_FROZEN", "Instance Frozen"
    INSTANCE_APPROVED = "INSTANCE_APPROVED", "Instance Approved"
    INSTANCE_REJECTED = "INSTANCE_REJECTED", "Instance Rejected"
    BRANCH_ASSIGNED = "BRANCH_ASSIGNED", "Branch Assigned"
    BRANCH_APPROVED = "BRANCH_APPROVED", "Branch Approved"
    BRANCH_REJECTED = "BRANCH_REJECTED", "Branch Rejected"
    BRANCH_REASSIGNED = "BRANCH_REASSIGNED", "Branch Reassigned"
    BRANCHES_SPLIT = "BRANCHES_SPLIT", "Branches Split"
    BRANCHES_JOINED = "BRANCHES_JOINED", "Branches Joined"
    SPLIT_ALLOCATIONS_SUBMITTED = "SPLIT_ALLOCATIONS_SUBMITTED", "Split Allocations Submitted"
    SPLIT_ALLOCATION_CORRECTED = "SPLIT_ALLOCATION_CORRECTED", "Split Allocation Corrected"
    SINGLE_ALLOCATION_SUBMITTED = "SINGLE_ALLOC_SUBMITTED", "Single Allocation Submitted"
    ALLOCATION_BUDGET_RESERVED = "ALLOCATION_BUDGET_RESERVED", "Allocation Budget Reserved"
    ALLOCATION_BUDGET_RELEASED = "ALLOCATION_BUDGET_RELEASED", "Allocation Budget Released"
    ALLOCATION_BUDGET_CONSUMED = "ALLOCATION_BUDGET_CONSUMED", "Allocation Budget Consumed"


class AssignmentState(models.TextChoices):
    """
    How a WorkflowInstanceStep arrived at its current assigned_user state.

    ASSIGNED           — user confirmed (via default_user, auto-assign, or manual pick).
    ASSIGNMENT_REQUIRED — multiple eligible users exist but no default; admin must pick.
    NO_ELIGIBLE_USERS  — zero users hold the required role at the resolved scope node;
                         workflow configuration must be fixed before this can proceed.
    """
    ASSIGNED = "ASSIGNED", "Assigned"
    ASSIGNMENT_REQUIRED = "ASSIGNMENT_REQUIRED", "Assignment Required"
    NO_ELIGIBLE_USERS = "NO_ELIGIBLE_USERS", "No Eligible Users"


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

class WorkflowTemplate(models.Model):
    """
    Identity of a workflow variant for a given module at a given node.
    Multiple templates (variants) can exist per (module, scope_node), each with a unique code.
    Versions are separate rows per template.

    code       — stable slug, unique per (module, scope_node). Auto-generated from name if blank.
    is_active  — inactive templates are hidden from eligible-workflows and auto-resolution.
    is_default — at most one default per (module, scope_node). Used by automatic resolve fallback.
    """
    name = models.CharField(max_length=255)
    code = models.SlugField(
        max_length=100,
        blank=True,
        help_text="Stable slug identifier, unique per module+scope_node. Auto-generated from name if blank.",
    )
    description = models.TextField(blank=True, default="")
    module = models.CharField(max_length=50, help_text="e.g. invoice, campaign, vendor, budget")
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.CASCADE,
        related_name="workflow_templates",
    )
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_workflow_templates",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workflow_templates"
        constraints = [
            models.UniqueConstraint(
                fields=["module", "scope_node", "code"],
                name="unique_template_code_per_module_per_node",
            ),
            models.UniqueConstraint(
                fields=["module", "scope_node"],
                condition=models.Q(is_default=True),
                name="unique_default_per_module_per_node",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.code and self.scope_node_id:
            from django.utils.text import slugify
            base = slugify(self.name)[:90] or "template"
            existing_codes = set(
                WorkflowTemplate.objects.filter(
                    module=self.module, scope_node_id=self.scope_node_id
                ).exclude(pk=self.pk).values_list("code", flat=True)
            )
            code = base
            i = 1
            while code in existing_codes:
                code = f"{base}-{i}"
                i += 1
            self.code = code
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} [{self.module} @ {self.scope_node}]"


class WorkflowTemplateVersion(models.Model):
    """
    A specific version of a WorkflowTemplate.
    Instances lock to a version row, not just a version number.

    DB constraints:
    - unique_version_per_template: (template, version_number) is unique.
    - unique_published_version_per_template: partial unique index — only one row
      with status='published' is allowed per template. Enforced at DB level.
    """
    template = models.ForeignKey(
        WorkflowTemplate,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20,
        choices=VersionStatus.choices,
        default=VersionStatus.DRAFT,
    )
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_template_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "workflow_template_versions"
        constraints = [
            models.UniqueConstraint(
                fields=["template", "version_number"],
                name="unique_version_per_template",
            ),
            models.UniqueConstraint(
                fields=["template"],
                condition=models.Q(status="published"),
                name="unique_published_version_per_template",
            ),
        ]
        indexes = [
            models.Index(fields=["template", "status"]),
        ]

    def __str__(self):
        return f"{self.template.name} v{self.version_number} [{self.status}]"


# ---------------------------------------------------------------------------
# Step Groups and Steps
# ---------------------------------------------------------------------------

class StepGroup(models.Model):
    """
    A group of steps within a template version. Groups execute sequentially;
    steps within a group execute according to parallel_mode.

    on_rejection_goto_group references group_id (stable), never display_order.
    """
    template_version = models.ForeignKey(
        WorkflowTemplateVersion,
        on_delete=models.CASCADE,
        related_name="step_groups",
    )
    name = models.CharField(max_length=255)
    display_order = models.PositiveIntegerField()
    parallel_mode = models.CharField(
        max_length=30,
        choices=ParallelMode.choices,
        default=ParallelMode.SINGLE,
    )
    on_rejection_action = models.CharField(
        max_length=20,
        choices=RejectionAction.choices,
        default=RejectionAction.TERMINATE,
    )
    on_rejection_goto_group = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rejection_sources",
        help_text="Target group on rejection if action=GO_TO_GROUP. References stable group id.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "step_groups"
        constraints = [
            models.UniqueConstraint(
                fields=["template_version", "display_order"],
                name="unique_group_order_per_version",
            ),
        ]
        ordering = ["display_order"]

    def __str__(self):
        return f"Group {self.display_order}: {self.name} [{self.template_version}]"


class WorkflowStep(models.Model):
    """
    A single step within a StepGroup.

    scope_resolution_policy determines which node's user pool is eligible:
        SUBJECT_NODE      → exact subject node
        ANCESTOR_OF_TYPE  → walk up to first ancestor of ancestor_node_type
        ORG_ROOT          → always org root
        FIXED_NODE        → always fixed_scope_node regardless of subject

    default_user is optional convenience only — never the authority source.
    Validated at instance creation; flagged if ineligible at that point.
    """
    group = models.ForeignKey(
        StepGroup,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    name = models.CharField(max_length=255)
    required_role = models.ForeignKey(
        "access.Role",
        on_delete=models.PROTECT,
        related_name="workflow_steps",
    )
    scope_resolution_policy = models.CharField(
        max_length=30,
        choices=ScopeResolutionPolicy.choices,
    )
    ancestor_node_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Used only when policy=ANCESTOR_OF_TYPE",
    )
    fixed_scope_node = models.ForeignKey(
        "core.ScopeNode",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fixed_workflow_steps",
        help_text="Used only when policy=FIXED_NODE",
    )
    default_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_workflow_steps",
        help_text="Optional convenience — validated at instance creation, not authority source",
    )
    display_order = models.PositiveIntegerField(default=0)
    step_kind = models.CharField(
        max_length=30,
        choices=StepKind.choices,
        default=StepKind.NORMAL_APPROVAL,
        help_text="Controls whether this step is a normal approval or a split/join step",
    )
    split_target_nodes = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Used when step_kind=SPLIT_BY_SCOPE. "
            "List of scope_node IDs that will each receive one branch. "
            "Snapshotted at split activation time — not a live reference."
        ),
    )
    split_target_mode = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text="EXPLICIT_NODES = use split_target_nodes list; CHILD_NODES = use direct children of subject node",
    )
    join_policy = models.CharField(
        max_length=30,
        choices=JoinPolicy.choices,
        blank=True,
        default="",
        help_text="Used when step_kind=JOIN_BRANCHES to determine when join completes",
    )
    # --- Runtime split allocation config (step_kind=RUNTIME_SPLIT_ALLOCATION) ---
    allocation_total_policy = models.CharField(
        max_length=30,
        choices=AllocationTotalPolicy.choices,
        default=AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL,
        help_text="Validation rule for total allocated amount vs invoice amount",
    )
    approver_selection_mode = models.CharField(
        max_length=30,
        choices=AssignmentMode.choices,
        default=AssignmentMode.RUNTIME_SELECTED_FROM_POOL,
        help_text="How branch approvers are selected at split time",
    )
    require_category = models.BooleanField(default=False, help_text="Allocation must have a budget category")
    require_subcategory = models.BooleanField(default=False, help_text="Allocation must have a budget subcategory")
    require_budget = models.BooleanField(default=False, help_text="Allocation must be linked to a budget")
    require_campaign = models.BooleanField(default=False, help_text="Allocation must be linked to a campaign")
    allow_multiple_lines_per_entity = models.BooleanField(
        default=False,
        help_text="Allow more than one allocation row for the same entity",
    )
    branch_approval_policy = models.CharField(
        max_length=30,
        choices=BranchApprovalPolicy.choices,
        default=BranchApprovalPolicy.REQUIRED_FOR_ALL,
        help_text=(
            "Controls whether branches from this split step require explicit approval. "
            "Only applies when step_kind=RUNTIME_SPLIT_ALLOCATION."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "workflow_steps"
        ordering = ["display_order"]

    def __str__(self):
        return f"Step: {self.name} [{self.group}]"


# ---------------------------------------------------------------------------
# Runtime Split Allocation Config
# ---------------------------------------------------------------------------

class WorkflowSplitOption(models.Model):
    """
    Per-entity approver pool and budget config for a RUNTIME_SPLIT_ALLOCATION step.
    Configures which entities are available, which roles/users can approve each entity,
    and optional default budget/category/campaign mappings.
    """
    workflow_step = models.ForeignKey(
        WorkflowStep,
        on_delete=models.CASCADE,
        related_name="split_options",
    )
    entity = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.CASCADE,
        related_name="split_options",
    )
    approver_role = models.ForeignKey(
        "access.Role",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="split_options",
    )
    allowed_approvers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="allowed_split_options",
        help_text="Explicit approver pool; overrides role if both are set",
    )
    category = models.ForeignKey(
        "budgets.BudgetCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="split_options",
    )
    subcategory = models.ForeignKey(
        "budgets.BudgetSubCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="split_options",
    )
    campaign = models.ForeignKey(
        "campaigns.Campaign",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="split_options",
    )
    budget = models.ForeignKey(
        "budgets.Budget",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="split_options",
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "workflow_split_options"
        ordering = ["display_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_step", "entity"],
                name="unique_split_option_per_step_per_entity",
            ),
        ]
        indexes = [
            models.Index(fields=["workflow_step", "is_active"]),
        ]

    def __str__(self):
        return f"SplitOption step={self.workflow_step_id} entity={self.entity_id}"


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------

class WorkflowInstance(models.Model):
    """
    A running instance of a workflow for a specific subject.
    Locked to a template_version at creation — config changes mid-flight are safe.
    subject_scope_node_id is denormalized for fast lookups without joining subject tables.

    template is intentionally NOT stored as a separate FK. It is always reachable via
    template_version.template. Storing both would create an integrity gap (no DB constraint
    can enforce that instance.template == instance.template_version.template).
    Use the .template property for access.
    """
    template_version = models.ForeignKey(
        WorkflowTemplateVersion,
        on_delete=models.PROTECT,
        related_name="instances",
        help_text="Locked at creation. Never changes.",
    )

    @property
    def template(self):
        """Derived from template_version — always consistent."""
        return self.template_version.template
    subject_type = models.CharField(max_length=50, help_text="e.g. invoice, campaign, vendor, budget")
    subject_id = models.PositiveBigIntegerField(help_text="PK of the subject record")
    subject_scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="workflow_instances",
        help_text="Denormalized from subject for fast scope checks",
    )
    status = models.CharField(
        max_length=20,
        choices=InstanceStatus.choices,
        default=InstanceStatus.DRAFT,
    )
    current_group = models.ForeignKey(
        "WorkflowInstanceGroup",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="started_workflow_instances",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workflow_instances"
        indexes = [
            models.Index(fields=["subject_type", "subject_id"]),
            models.Index(fields=["subject_scope_node", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Instance {self.id} [{self.subject_type}:{self.subject_id}] {self.status}"


class WorkflowInstanceGroup(models.Model):
    """Runtime copy of a StepGroup for a specific instance."""
    instance = models.ForeignKey(
        WorkflowInstance,
        on_delete=models.CASCADE,
        related_name="instance_groups",
    )
    step_group = models.ForeignKey(
        StepGroup,
        on_delete=models.PROTECT,
        related_name="instance_groups",
    )
    display_order = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20,
        choices=GroupStatus.choices,
        default=GroupStatus.WAITING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workflow_instance_groups"
        ordering = ["display_order"]

    def __str__(self):
        return f"InstanceGroup {self.id} order={self.display_order} [{self.status}]"


class WorkflowInstanceStep(models.Model):
    """
    Runtime copy of a WorkflowStep for a specific instance group.
    assigned_user is the person responsible for acting on this step.
    Reassignment fields track the full audit trail.
    """
    instance_group = models.ForeignKey(
        WorkflowInstanceGroup,
        on_delete=models.CASCADE,
        related_name="instance_steps",
    )
    workflow_step = models.ForeignKey(
        WorkflowStep,
        on_delete=models.PROTECT,
        related_name="instance_steps",
    )
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_workflow_steps",
    )
    assignment_state = models.CharField(
        max_length=30,
        choices=AssignmentState.choices,
        default=AssignmentState.ASSIGNMENT_REQUIRED,
        help_text=(
            "How this step's assigned_user was resolved at instance creation. "
            "ASSIGNED = user confirmed; ASSIGNMENT_REQUIRED = multiple candidates, pick manually; "
            "NO_ELIGIBLE_USERS = no users hold the required role at the resolved scope node."
        ),
    )
    status = models.CharField(
        max_length=20,
        choices=StepStatus.choices,
        default=StepStatus.WAITING,
    )
    acted_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    reassigned_from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reassigned_from_steps",
    )
    reassigned_at = models.DateTimeField(null=True, blank=True)
    reassigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reassigned_by_steps",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workflow_instance_steps"
        constraints = [
            models.UniqueConstraint(
                fields=["instance_group", "workflow_step"],
                name="unique_step_per_instance_group",
            ),
        ]

    def __str__(self):
        return f"InstanceStep {self.id} [{self.status}]"


# ---------------------------------------------------------------------------
# Branch runtime (split/join)
# ---------------------------------------------------------------------------

class WorkflowInstanceBranch(models.Model):
    """
    Runtime model for one branch of a SPLIT_BY_SCOPE step.

    One branch exists per target scope node resolved at split activation time.
    Branch set is frozen — future org changes do not alter running branches.

    The parent WorkflowInstanceStep with step_kind=SPLIT_BY_SCOPE remains
    WAITING while any branch is PENDING. When all branches reach final state,
    the parent step advances (join logic) and branches are closed.
    """
    parent_instance_step = models.ForeignKey(
        WorkflowInstanceStep,
        on_delete=models.CASCADE,
        related_name="branches",
        help_text="The SPLIT_BY_SCOPE step that created this branch",
    )
    instance = models.ForeignKey(
        WorkflowInstance,
        on_delete=models.CASCADE,
        related_name="branches",
        help_text="Back-reference to parent instance for fast querying",
    )
    target_scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="workflow_branches",
        help_text="Frozen at split activation — the unit responsible for this branch",
    )
    branch_index = models.PositiveIntegerField(
        default=0,
        help_text="Order of this branch among siblings (for display)",
    )
    status = models.CharField(
        max_length=20,
        choices=BranchStatus.choices,
        default=BranchStatus.PENDING,
    )
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_workflow_branches",
    )
    assignment_state = models.CharField(
        max_length=30,
        choices=AssignmentState.choices,
        default=AssignmentState.ASSIGNMENT_REQUIRED,
    )
    acted_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    reassigned_from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reassigned_from_branches",
    )
    reassigned_at = models.DateTimeField(null=True, blank=True)
    reassigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reassigned_by_branches",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workflow_instance_branches"
        ordering = ["branch_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["parent_instance_step", "target_scope_node"],
                name="unique_branch_per_step_per_node",
            ),
        ]

    def __str__(self):
        return f"Branch {self.id} node={self.target_scope_node_id} [{self.status}]"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class WorkflowEvent(models.Model):
    """
    Immutable audit/event log for workflow activity.
    Business events are stored here; delivery channels are in NotificationDelivery.
    """
    instance = models.ForeignKey(
        WorkflowInstance,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_type = models.CharField(max_length=30, choices=WorkflowEventType.choices)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="actor_workflow_events",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="target_workflow_events",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "workflow_events"
        indexes = [
            models.Index(fields=["instance", "event_type"]),
            models.Index(fields=["instance", "created_at"]),
        ]

    def __str__(self):
        return f"Event {self.event_type} on Instance {self.instance_id}"
