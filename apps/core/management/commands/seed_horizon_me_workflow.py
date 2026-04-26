"""
Idempotent seed command: Horizon Marketing-Executive invoice workflow templates.

Fixes defects from the initial implementation:
  1. Templates were seeded at `corporate` (sibling of regional nodes, not an ancestor).
     Route discovery walks [invoice.scope_node + ancestors], so corporate templates
     are invisible for north/south/west/incity invoices. Templates now live at the
     `marketing` department node (depth=0, parent=None), which IS an ancestor of
     all regional nodes.
  2. ME role assignments were at individual park nodes. Step 1 uses FIXED_NODE
     scope resolution pointing to `marketing`, so MEs must hold the
     `marketing_executive` role AT the marketing node to be eligible.
  3. Step 1 uses default_user on the step to route to the correct ME for that
     park's template (me1 -> marketingexecutive1@horizon.local, etc.).

Templates (all scoped to Horizon marketing node):
  invoice-3-step-me1   3-step: ME1 Allocation -> Marketing Head -> HO Head
  invoice-3-step-me2   3-step: ME2 Allocation -> Marketing Head -> HO Head
  invoice-3-step-me3   3-step: ME3 Allocation -> Marketing Head -> HO Head
  invoice-4-step-me4   4-step: ME4 Allocation -> Marketing Head -> HO Executive -> HO Head

Usage:
    python manage.py seed_horizon_me_workflow
    python manage.py seed_horizon_me_workflow --dry-run
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.access.models import (
    Permission,
    PermissionAction,
    PermissionResource,
    Role,
    RolePermission,
    UserRoleAssignment,
)
from apps.core.models import Organization, ScopeNode
from apps.workflow.models import (
    AllocationTotalPolicy,
    BranchApprovalPolicy,
    ParallelMode,
    RejectionAction,
    ScopeResolutionPolicy,
    StepGroup,
    StepKind,
    VersionStatus,
    WorkflowStep,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)

User = get_user_model()

_HORIZON_ORG_CODE = "horizon"
_PASSWORD = "Password@123"

# Park code -> ME user email + display suffix
_ME_USERS = [
    ("north",  "marketingexecutive1@horizon.local", "Marketing Executive", "1"),
    ("south",  "marketingexecutive2@horizon.local", "Marketing Executive", "2"),
    ("west",   "marketingexecutive3@horizon.local", "Marketing Executive", "3"),
    ("incity", "marketingexecutive4@horizon.local", "Marketing Executive", "4"),
]

# (template_code, display_name, park_code, extra_ho_executive_step)
_TEMPLATE_DEFS = [
    ("invoice-3-step-me1", "Invoice 3-Step ME1", "north",  False),
    ("invoice-3-step-me2", "Invoice 3-Step ME2", "south",  False),
    ("invoice-3-step-me3", "Invoice 3-Step ME3", "west",   False),
    ("invoice-4-step-me4", "Invoice 4-Step ME4", "incity", True),
]


def _grant(role: Role, action: str, resource: str) -> None:
    perm, _ = Permission.objects.get_or_create(action=action, resource=resource)
    RolePermission.objects.get_or_create(role=role, permission=perm)


class Command(BaseCommand):
    help = "Seed Horizon ME invoice workflow templates (idempotent, corrects prior bad data)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be done without writing to the database.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry-run mode — no DB writes."))

        # 1. Horizon org
        try:
            org = Organization.objects.get(code=_HORIZON_ORG_CODE)
        except Organization.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(
                    f"Organization '{_HORIZON_ORG_CODE}' not found. "
                    "Run seed_horizon_marketing_budget first."
                )
            )
            return

        self._write(f"Org: {org.name}", dry_run)

        # 2. Scope nodes
        try:
            marketing = ScopeNode.objects.get(org=org, code="marketing")
        except ScopeNode.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(
                    "ScopeNode 'marketing' not found. "
                    "Run seed_horizon_marketing_budget first."
                )
            )
            return
        self._write(f"marketing node: id={marketing.id}, path={marketing.path}", dry_run)

        # 3. Normalize bad data before creating new rows
        if not dry_run:
            self._normalize_bad_data(org)
        else:
            self._write(
                "[DRY-RUN] Would delete 4 seeded ME users' assignments at park nodes",
                dry_run,
            )
            self._write(
                "[DRY-RUN] Would delete 4 ME template codes at corporate node",
                dry_run,
            )

        # 4. Roles
        roles = self._ensure_roles(org, dry_run)

        # 5. ME users + role assignments at marketing node
        me_users = self._ensure_me_users(org, marketing, roles, dry_run)

        # 6. Templates at marketing node
        if not dry_run:
            admin = User.objects.filter(is_staff=True, email__endswith="@horizon.local").first()
            if admin is None:
                admin = User.objects.filter(is_staff=True).first()
        else:
            admin = None

        self._ensure_templates(org, marketing, roles, me_users, admin, dry_run)

        if not dry_run:
            self.stdout.write(self.style.SUCCESS("Done. Horizon ME workflow templates seeded."))
        else:
            self.stdout.write(self.style.SUCCESS("Dry-run complete."))

    # ── Normalize bad data ─────────────────────────────────────────────────────

    def _normalize_bad_data(self, org):
        """
        Delete ONLY the known-bad data introduced by the earlier incorrect seed.
        Does NOT delete unrelated legitimate assignments.
        """
        corporate_nodes = ScopeNode.objects.filter(org=org, code="corporate")
        park_codes = {"north", "south", "west", "incity"}
        me_role = Role.objects.filter(org=org, code="marketing_executive").first()
        known_bad_emails = {
            "marketingexecutive1@horizon.local",
            "marketingexecutive2@horizon.local",
            "marketingexecutive3@horizon.local",
            "marketingexecutive4@horizon.local",
        }

        # Delete ONLY the 4 seeded ME users' assignments at wrong park nodes
        if me_role:
            deleted_assignments = UserRoleAssignment.objects.filter(
                role=me_role,
                scope_node__code__in=park_codes,
                user__email__in=known_bad_emails,
            ).delete()
            if deleted_assignments[0]:
                self._write(
                    f"  Deleted {deleted_assignments[0]} bad ME role assignment(s) "
                    f"(seeded users only) at park nodes",
                    False,
                )

        # Delete ONLY the 4 known-bad ME template codes at corporate scope
        deleted_templates = WorkflowTemplate.objects.filter(
            scope_node__in=corporate_nodes,
            module="invoice",
            code__in=["invoice-3-step-me1", "invoice-3-step-me2",
                      "invoice-3-step-me3", "invoice-4-step-me4"],
        ).delete()
        if deleted_templates[0]:
            self._write(f"  Deleted {deleted_templates[0]} bad template(s) at corporate", False)

    # ── Roles ─────────────────────────────────────────────────────────────────

    def _ensure_roles(self, org, dry_run):
        role_specs = [
            ("marketing_executive", "Marketing Executive"),
            ("marketing_head",      "Marketing Head"),
            ("ho_executive",        "HO Executive"),
            ("ho_head",             "HO Head"),
        ]
        roles = {}
        for code, name in role_specs:
            if not dry_run:
                role, created = Role.objects.get_or_create(
                    org=org, code=code,
                    defaults={"name": name, "is_active": True},
                )
                if created:
                    _grant(role, PermissionAction.READ.value,   PermissionResource.INVOICE.value)
                    _grant(role, PermissionAction.APPROVE.value, PermissionResource.INVOICE.value)
                roles[code] = role
            self._write(f"Role: {code}", dry_run)
        return roles

    # ── ME Users + Assignments at marketing node ────────────────────────────────

    def _ensure_me_users(self, org, marketing_node, roles, dry_run):
        me_role = roles.get("marketing_executive") if not dry_run else None
        me_users = {}
        for park_code, email, first, suffix in _ME_USERS:
            if not dry_run:
                user, created = User.objects.get_or_create(
                    email=email,
                    defaults={
                        "first_name": first,
                        "last_name": suffix,
                        "is_active": True,
                        "is_staff": False,
                    },
                )
                user.set_password(_PASSWORD)
                user.save(update_fields=["password"])
                # Assign at marketing node (not individual park nodes)
                UserRoleAssignment.objects.get_or_create(
                    user=user,
                    role=me_role,
                    scope_node=marketing_node,
                )
                me_users[park_code] = user
            self._write(f"User {email} -> marketing_executive @ marketing", dry_run)
        return me_users

    # ── Templates ──────────────────────────────────────────────────────────────

    def _ensure_templates(self, org, marketing_node, roles, me_users, admin, dry_run):
        for template_code, display_name, park_code, has_ho_executive in _TEMPLATE_DEFS:
            self._write(f"\nTemplate: {template_code}", dry_run)

            if dry_run:
                self._write(f"  Scope: marketing (FIXED_NODE for all steps)", dry_run)
                self._write(f"  Step 1: ME Allocation (RUNTIME_SPLIT_ALLOCATION/SKIP_ALL) @ marketing", dry_run)
                self._write(f"  Step 2: Marketing Head Review (NORMAL_APPROVAL) @ marketing", dry_run)
                if has_ho_executive:
                    self._write("  Step 3: HO Executive Review (NORMAL_APPROVAL) @ marketing", dry_run)
                self._write(
                    f"  Step {'4' if has_ho_executive else '3'}: HO Head Review (NORMAL_APPROVAL) @ marketing",
                    dry_run,
                )
                continue

            # Template at marketing node
            template, t_created = WorkflowTemplate.objects.get_or_create(
                module="invoice",
                scope_node=marketing_node,
                code=template_code,
                defaults={"name": display_name, "created_by": admin, "is_active": True},
            )
            self._write(f"  Template {'created' if t_created else 'exists'}: {template_code} @ marketing", dry_run)

            # Version
            version, v_created = WorkflowTemplateVersion.objects.get_or_create(
                template=template,
                version_number=1,
                defaults={"status": VersionStatus.PUBLISHED},
            )
            if not v_created and version.status != VersionStatus.PUBLISHED:
                version.status = VersionStatus.PUBLISHED
                version.save(update_fields=["status"])
            self._write(f"  Version {'created' if v_created else 'exists'}: v1", dry_run)

            # Build step group definitions
            groups_def = self._build_groups_def(
                roles, marketing_node, me_users, park_code, has_ho_executive,
            )
            self._ensure_groups_and_steps(version, groups_def)

    def _build_groups_def(self, roles, marketing_node, me_users, park_code, has_ho_executive):
        """Build (group_kwargs, step_kwargs) list for this template."""
        me_user = me_users.get(park_code)
        order = 1
        groups = []

        # Step 1: ME Allocation (RUNTIME_SPLIT_ALLOCATION)
        groups.append({
            "group": {
                "name": "ME Allocation",
                "display_order": order,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.RETURN_TO_SUBMITTER,
            },
            "step": {
                "name": "ME Allocation",
                "display_order": 1,
                "required_role": roles["marketing_executive"],
                "scope_resolution_policy": ScopeResolutionPolicy.FIXED_NODE,
                "fixed_scope_node": marketing_node,
                "default_user": me_user,  # route to the correct ME for this park
                "step_kind": StepKind.RUNTIME_SPLIT_ALLOCATION,
                "branch_approval_policy": BranchApprovalPolicy.SKIP_ALL,
                "allocation_total_policy": AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL,
                "require_budget": True,
                "require_category": True,
                "require_subcategory": True,
                "allow_multiple_lines_per_entity": True,
            },
        })
        order += 1

        # Step 2: Marketing Head (all at marketing node)
        groups.append({
            "group": {
                "name": "Marketing Head Review",
                "display_order": order,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.TERMINATE,
            },
            "step": {
                "name": "Marketing Head Review",
                "display_order": 1,
                "required_role": roles["marketing_head"],
                "scope_resolution_policy": ScopeResolutionPolicy.FIXED_NODE,
                "fixed_scope_node": marketing_node,
                "step_kind": StepKind.NORMAL_APPROVAL,
            },
        })
        order += 1

        # Step 3 (4-step only): HO Executive
        if has_ho_executive:
            groups.append({
                "group": {
                    "name": "HO Executive Review",
                    "display_order": order,
                    "parallel_mode": ParallelMode.SINGLE,
                    "on_rejection_action": RejectionAction.TERMINATE,
                },
                "step": {
                    "name": "HO Executive Review",
                    "display_order": 1,
                    "required_role": roles["ho_executive"],
                    "scope_resolution_policy": ScopeResolutionPolicy.FIXED_NODE,
                    "fixed_scope_node": marketing_node,
                    "step_kind": StepKind.NORMAL_APPROVAL,
                },
            })
            order += 1

        # Last step: HO Head
        groups.append({
            "group": {
                "name": "HO Head Review",
                "display_order": order,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.TERMINATE,
            },
            "step": {
                "name": "HO Head Review",
                "display_order": 1,
                "required_role": roles["ho_head"],
                "scope_resolution_policy": ScopeResolutionPolicy.FIXED_NODE,
                "fixed_scope_node": marketing_node,
                "step_kind": StepKind.NORMAL_APPROVAL,
            },
        })

        return groups

    def _ensure_groups_and_steps(self, version, groups_def):
        _ALLOCATION_FIELDS = [
            "step_kind", "branch_approval_policy", "allocation_total_policy",
            "require_budget", "require_category", "require_subcategory",
            "allow_multiple_lines_per_entity", "fixed_scope_node", "scope_resolution_policy",
            "required_role", "default_user",
        ]

        for g_def in groups_def:
            step_def = g_def["step"]
            group_kwargs = g_def["group"]

            group, g_created = StepGroup.objects.get_or_create(
                template_version=version,
                display_order=group_kwargs["display_order"],
                defaults=group_kwargs,
            )
            if not g_created:
                for field, value in group_kwargs.items():
                    setattr(group, field, value)
                group.save(update_fields=list(group_kwargs.keys()))
            self._write(f"    Group {'created' if g_created else 'updated'}: {group.name}", dry_run=False)

            step, s_created = WorkflowStep.objects.get_or_create(
                group=group,
                display_order=step_def["display_order"],
                defaults=step_def,
            )
            if not s_created:
                for field in _ALLOCATION_FIELDS:
                    if field in step_def:
                        setattr(step, field, step_def[field])
                step.save(update_fields=[f for f in _ALLOCATION_FIELDS if f in step_def])
            self._write(f"      Step {'created' if s_created else 'updated'}: {step.name}", dry_run=False)

    def _write(self, msg, dry_run):
        prefix = "[DRY-RUN] " if dry_run else "  "
        self.stdout.write(f"{prefix}{msg}")
