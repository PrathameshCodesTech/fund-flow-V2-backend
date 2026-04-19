"""
Idempotent V2 demo seed command.

Usage:
    python manage.py seed_v2_demo          # idempotent — creates missing records
    python manage.py seed_v2_demo --reset  # wipe all demo-owned data then reseed

Reset scope (only touches demo-owned identifiers):
    - Users with @demo.local emails
    - Organization code='demo-org' and ALL dependent records

UAT users (all password: Password@123):
    tenantadmin@demo.local      — Org Admin (full access)
    entitymanager.x@demo.local  — Entity Manager at Entity X  (invoice step 1 for X)
    entitymanager.y@demo.local  — Entity Manager at Entity Y  (invoice step 1 for Y)
    marketinghead@demo.local    — Marketing Head at Company A  (invoice step 2)
    ho@demo.local               — HO Head at Company A         (invoice step 3)
    finance@demo.local          — Finance Team at Company A    (invoice step 4 + external handoff target)

Invoice workflow step order (hierarchy = lowest unit first):
    Step 1  entity_manager    SUBJECT_NODE           -> Entity X or Entity Y manager
    Step 2  marketing_head    ANCESTOR_OF_TYPE(company) -> Company A
    Step 3  ho_head           ANCESTOR_OF_TYPE(company) -> Company A
    Step 4  finance_team      ORG_ROOT               -> Company A [then external FinanceHandoff auto-fires]
"""
import secrets
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.access.models import (
    Permission,
    PermissionAction,
    PermissionResource,
    Role,
    RolePermission,
    UserRoleAssignment,
)
from apps.budgets.models import (
    Budget,
    BudgetCategory,
    BudgetRule,
    BudgetStatus,
    BudgetSubCategory,
    PeriodType,
)
from apps.campaigns.models import Campaign, CampaignStatus
from apps.core.models import NodeType, Organization, ScopeNode
from apps.invoices.models import Invoice, InvoiceStatus
from apps.modules.models import ModuleActivation, ModuleType
from apps.vendors.models import (
    InvitationStatus,
    MarketingStatus,
    OperationalStatus,
    SubmissionStatus,
    Vendor,
    VendorInvitation,
    VendorOnboardingSubmission,
)
from apps.workflow.models import (
    ParallelMode,
    RejectionAction,
    ScopeResolutionPolicy,
    StepGroup,
    VersionStatus,
    WorkflowInstance,
    WorkflowInstanceGroup,
    WorkflowInstanceStep,
    WorkflowStep,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)

User = get_user_model()

_DEMO_ORG_CODE = "demo-org"
_DEMO_SUFFIX = "@demo.local"
_PASSWORD = "Password@123"


def _grant(role: Role, action: str, resource: str) -> None:
    perm, _ = Permission.objects.get_or_create(action=action, resource=resource)
    RolePermission.objects.get_or_create(role=role, permission=perm)


class Command(BaseCommand):
    help = (
        "Seed UAT-ready demo data aligned to the full business flow "
        "(vendor onboarding -> invoice hierarchy approval -> external finance)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Delete all demo-owned data before reseeding. "
                "Scoped to demo-org and @demo.local users only."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options["reset"]:
            self._reset()
        self._seed()

    # ── Reset ─────────────────────────────────────────────────────────────────

    def _reset(self):
        self.stdout.write(self.style.WARNING("-- Resetting demo data --"))

        org = Organization.objects.filter(code=_DEMO_ORG_CODE).first()
        if org:
            nodes = ScopeNode.objects.filter(org=org)
            node_ids = list(nodes.values_list("id", flat=True))

            # 1. Workflow instances (PROTECT on subject_scope_node + template_version)
            inst_ids = list(
                WorkflowInstance.objects.filter(
                    subject_scope_node_id__in=node_ids
                ).values_list("id", flat=True)
            )
            WorkflowInstanceStep.objects.filter(
                instance_group__instance_id__in=inst_ids
            ).delete()
            WorkflowInstanceGroup.objects.filter(
                instance_id__in=inst_ids
            ).delete()
            WorkflowInstance.objects.filter(id__in=inst_ids).delete()

            # 2. Generic finance handoffs
            try:
                from apps.finance.models import (
                    FinanceActionToken,
                    FinanceDecision,
                    FinanceHandoff,
                )
                hids = list(
                    FinanceHandoff.objects.filter(
                        scope_node_id__in=node_ids
                    ).values_list("id", flat=True)
                )
                FinanceDecision.objects.filter(handoff_id__in=hids).delete()
                FinanceActionToken.objects.filter(handoff_id__in=hids).delete()
                FinanceHandoff.objects.filter(id__in=hids).delete()
            except ImportError:
                pass

            # 3. Domain objects that PROTECT scope_node
            Invoice.objects.filter(scope_node_id__in=node_ids).delete()
            Campaign.objects.filter(scope_node_id__in=node_ids).delete()
            Vendor.objects.filter(scope_node_id__in=node_ids).delete()

            # 4. Vendor invitations + submissions (invitation PROTECT on org)
            inv_ids = list(
                VendorInvitation.objects.filter(
                    scope_node_id__in=node_ids
                ).values_list("id", flat=True)
            )
            VendorOnboardingSubmission.objects.filter(
                invitation_id__in=inv_ids
            ).delete()
            VendorInvitation.objects.filter(id__in=inv_ids).delete()

            # 5. Budgets, templates, module activations, role assignments at nodes
            Budget.objects.filter(scope_node_id__in=node_ids).delete()
            WorkflowTemplate.objects.filter(
                scope_node_id__in=node_ids
            ).delete()  # cascades -> versions -> groups -> steps
            ModuleActivation.objects.filter(scope_node_id__in=node_ids).delete()
            UserRoleAssignment.objects.filter(scope_node_id__in=node_ids).delete()

            # 6. Scope nodes (PROTECT children all cleared above)
            nodes.delete()

            # 7. Roles (WorkflowStep PROTECT on required_role — steps deleted above)
            role_ids = list(
                Role.objects.filter(org=org).values_list("id", flat=True)
            )
            RolePermission.objects.filter(role_id__in=role_ids).delete()
            Role.objects.filter(id__in=role_ids).delete()

            # 8. Budget categories + org
            BudgetCategory.objects.filter(org=org).delete()
            org.delete()
            self.stdout.write("  Deleted demo-org and all dependent records.")
        else:
            self.stdout.write("  No demo-org found — nothing to delete.")

        # 9. @demo.local users (cascade-deletes remaining assignments)
        deleted_count, _ = User.objects.filter(
            email__endswith=_DEMO_SUFFIX
        ).delete()
        self.stdout.write(f"  Deleted {deleted_count} @demo.local user(s).")
        self.stdout.write(self.style.SUCCESS("Reset complete. Reseeding now...\n"))

    # ── Seed ──────────────────────────────────────────────────────────────────

    def _seed(self):
        self.stdout.write("Seeding UAT demo data...")

        org = self._org()
        company_a, entity_x, entity_y = self._nodes(org)
        users = self._users()
        roles = self._roles(org)
        self._role_assignments(users, roles, company_a, entity_x, entity_y)
        self._module_activations(company_a, entity_x, entity_y)
        self._invoice_workflow(company_a, roles, users)
        self._campaign_workflow(company_a, roles, users)
        mkt_cat, digital_ads = self._budgets(org, company_a, entity_x, entity_y, users)
        self._demo_campaign(org, entity_x, mkt_cat, digital_ads, users)
        self._demo_invoice(entity_x, users)
        self._vendors(org, entity_x, users)
        self._print_summary(org, company_a, entity_x, entity_y)

    # ── Organization ──────────────────────────────────────────────────────────

    def _org(self):
        org, created = Organization.objects.get_or_create(
            code=_DEMO_ORG_CODE,
            defaults={"name": "Demo Organization"},
        )
        self.stdout.write(f"  {'Created' if created else 'Exists '} org: {org.name}")
        return org

    # ── Scope Hierarchy ───────────────────────────────────────────────────────

    def _nodes(self, org):
        company_a, created = ScopeNode.objects.get_or_create(
            org=org,
            code="company-a",
            defaults={
                "name": "Company A",
                "node_type": NodeType.COMPANY,
                "parent": None,
                "path": f"/{org.code}/company-a",
                "depth": 0,
                "is_active": True,
            },
        )
        self.stdout.write(f"  {'Created' if created else 'Exists '} node: {company_a.name}")

        entity_x, created = ScopeNode.objects.get_or_create(
            org=org,
            code="entity-x",
            defaults={
                "name": "Entity X",
                "node_type": NodeType.ENTITY,
                "parent": company_a,
                "path": f"/{org.code}/company-a/entity-x",
                "depth": 1,
                "is_active": True,
            },
        )
        self.stdout.write(f"  {'Created' if created else 'Exists '} node: {entity_x.name}")

        entity_y, created = ScopeNode.objects.get_or_create(
            org=org,
            code="entity-y",
            defaults={
                "name": "Entity Y",
                "node_type": NodeType.ENTITY,
                "parent": company_a,
                "path": f"/{org.code}/company-a/entity-y",
                "depth": 1,
                "is_active": True,
            },
        )
        self.stdout.write(f"  {'Created' if created else 'Exists '} node: {entity_y.name}")

        return company_a, entity_x, entity_y

    # ── Users ─────────────────────────────────────────────────────────────────

    def _users(self):
        specs = [
            ("tenantadmin@demo.local", "Tenant", "Admin", True),
            ("entitymanager.x@demo.local", "Entity Manager", "X", False),
            ("entitymanager.y@demo.local", "Entity Manager", "Y", False),
            ("marketinghead@demo.local", "Marketing", "Head", False),
            ("ho@demo.local", "HO", "Head", False),
            ("finance@demo.local", "Finance", "Lead", False),
        ]
        user_map = {}
        for email, first, last, is_staff in specs:
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "is_active": True,
                    "is_staff": is_staff,
                },
            )
            user.set_password(_PASSWORD)
            user.save(update_fields=["password"])
            self.stdout.write(f"  {'Created' if created else 'Exists '} user: {email}")
            user_map[email] = user
        return user_map

    # ── Roles + Permissions ───────────────────────────────────────────────────

    def _roles(self, org):
        def _role(code, name):
            role, created = Role.objects.get_or_create(
                org=org,
                code=code,
                defaults={"name": name, "is_active": True},
            )
            self.stdout.write(f"  {'Created' if created else 'Exists '} role: {name} ({code})")
            return role

        roles = {
            "org_admin": _role("org_admin", "Org Admin"),
            "entity_manager": _role("entity_manager", "Entity Manager"),
            "marketing_head": _role("marketing_head", "Marketing Head"),
            "ho_head": _role("ho_head", "HO Head"),
            "finance_team": _role("finance_team", "Finance Team"),
        }

        # Org admin — full access to everything
        for action in PermissionAction:
            for resource in PermissionResource:
                _grant(roles["org_admin"], action.value, resource.value)

        # Entity manager — create/read/approve invoices, start workflow
        for action in (PermissionAction.CREATE, PermissionAction.READ, PermissionAction.APPROVE):
            _grant(roles["entity_manager"], action.value, PermissionResource.INVOICE.value)
        _grant(
            roles["entity_manager"],
            PermissionAction.START_WORKFLOW.value,
            PermissionResource.INVOICE.value,
        )

        # Marketing head — read + approve invoices and campaigns
        for resource in (PermissionResource.INVOICE, PermissionResource.CAMPAIGN):
            _grant(roles["marketing_head"], PermissionAction.READ.value, resource.value)
            _grant(roles["marketing_head"], PermissionAction.APPROVE.value, resource.value)

        # HO head — read + approve invoices and campaigns
        for resource in (PermissionResource.INVOICE, PermissionResource.CAMPAIGN):
            _grant(roles["ho_head"], PermissionAction.READ.value, resource.value)
            _grant(roles["ho_head"], PermissionAction.APPROVE.value, resource.value)

        # Finance team — read + approve invoices and campaigns (step 4 internal gate)
        for resource in (PermissionResource.INVOICE, PermissionResource.CAMPAIGN):
            _grant(roles["finance_team"], PermissionAction.READ.value, resource.value)
            _grant(roles["finance_team"], PermissionAction.APPROVE.value, resource.value)

        self.stdout.write("  Permissions assigned to all roles.")
        return roles

    # ── Role Assignments ──────────────────────────────────────────────────────

    def _role_assignments(self, users, roles, company_a, entity_x, entity_y):
        assignments = [
            ("tenantadmin@demo.local", "org_admin", company_a),
            ("entitymanager.x@demo.local", "entity_manager", entity_x),
            ("entitymanager.y@demo.local", "entity_manager", entity_y),
            ("marketinghead@demo.local", "marketing_head", company_a),
            ("ho@demo.local", "ho_head", company_a),
            ("finance@demo.local", "finance_team", company_a),
        ]
        for email, role_key, node in assignments:
            _, created = UserRoleAssignment.objects.get_or_create(
                user=users[email],
                role=roles[role_key],
                scope_node=node,
            )
            self.stdout.write(
                f"  {'Assigned' if created else 'Exists '} "
                f"{role_key} -> {email} @ {node.code}"
            )

    # ── Module Activations ────────────────────────────────────────────────────

    def _module_activations(self, company_a, entity_x, entity_y):
        activations = [
            (ModuleType.INVOICE, company_a),
            (ModuleType.INVOICE, entity_x),
            (ModuleType.INVOICE, entity_y),
            (ModuleType.CAMPAIGN, company_a),
            (ModuleType.CAMPAIGN, entity_x),
            (ModuleType.CAMPAIGN, entity_y),
            (ModuleType.VENDOR, company_a),
            (ModuleType.BUDGET, company_a),
        ]
        for module, node in activations:
            act, created = ModuleActivation.objects.get_or_create(
                module=module,
                scope_node=node,
                defaults={"is_active": True, "override_parent": True},
            )
            if not created and not act.is_active:
                act.is_active = True
                act.save(update_fields=["is_active"])
            self.stdout.write(
                f"  {'Activated' if created else 'Exists  '} module {module} @ {node.code}"
            )

    # ── Invoice Workflow Template ─────────────────────────────────────────────

    def _invoice_workflow(self, company_a, roles, users):
        admin = users["tenantadmin@demo.local"]

        template, created = WorkflowTemplate.objects.get_or_create(
            scope_node=company_a,
            module="invoice",
            defaults={"name": "Invoice Approval Flow", "created_by": admin},
        )
        self.stdout.write(f"  {'Created' if created else 'Exists '} invoice workflow template")

        version, created = WorkflowTemplateVersion.objects.get_or_create(
            template=template,
            version_number=1,
            defaults={"status": VersionStatus.PUBLISHED},
        )
        if not created and version.status != VersionStatus.PUBLISHED:
            version.status = VersionStatus.PUBLISHED
            version.save(update_fields=["status"])
        self.stdout.write(f"  {'Created' if created else 'Exists '} invoice template v1 (published)")

        groups_def = [
            {
                "name": "Entity Manager Review",
                "display_order": 1,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.TERMINATE,
                "step": {
                    "name": "Entity Manager Review",
                    "required_role": roles["entity_manager"],
                    "scope_resolution_policy": ScopeResolutionPolicy.SUBJECT_NODE,
                    "display_order": 1,
                },
            },
            {
                "name": "Marketing Head Review",
                "display_order": 2,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.TERMINATE,
                "step": {
                    "name": "Marketing Head Review",
                    "required_role": roles["marketing_head"],
                    "scope_resolution_policy": ScopeResolutionPolicy.ANCESTOR_OF_TYPE,
                    "ancestor_node_type": NodeType.COMPANY.value,
                    "display_order": 1,
                },
            },
            {
                "name": "HO Head Review",
                "display_order": 3,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.TERMINATE,
                "step": {
                    "name": "HO Head Review",
                    "required_role": roles["ho_head"],
                    "scope_resolution_policy": ScopeResolutionPolicy.ANCESTOR_OF_TYPE,
                    "ancestor_node_type": NodeType.COMPANY.value,
                    "display_order": 1,
                },
            },
            {
                "name": "Finance Team Review",
                "display_order": 4,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.TERMINATE,
                "step": {
                    "name": "Finance Team Review",
                    "required_role": roles["finance_team"],
                    "scope_resolution_policy": ScopeResolutionPolicy.ORG_ROOT,
                    "display_order": 1,
                },
            },
        ]
        self._create_groups_and_steps(version, groups_def, "invoice")

    # ── Campaign Workflow Template ────────────────────────────────────────────

    def _campaign_workflow(self, company_a, roles, users):
        admin = users["tenantadmin@demo.local"]

        template, created = WorkflowTemplate.objects.get_or_create(
            scope_node=company_a,
            module="campaign",
            defaults={"name": "Campaign Approval Flow", "created_by": admin},
        )
        self.stdout.write(f"  {'Created' if created else 'Exists '} campaign workflow template")

        version, created = WorkflowTemplateVersion.objects.get_or_create(
            template=template,
            version_number=1,
            defaults={"status": VersionStatus.PUBLISHED},
        )
        if not created and version.status != VersionStatus.PUBLISHED:
            version.status = VersionStatus.PUBLISHED
            version.save(update_fields=["status"])
        self.stdout.write(f"  {'Created' if created else 'Exists '} campaign template v1 (published)")

        groups_def = [
            {
                "name": "Entity Manager Approval",
                "display_order": 1,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.TERMINATE,
                "step": {
                    "name": "Entity Manager Review",
                    "required_role": roles["entity_manager"],
                    "scope_resolution_policy": ScopeResolutionPolicy.SUBJECT_NODE,
                    "display_order": 1,
                },
            },
            {
                "name": "Marketing Head Approval",
                "display_order": 2,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.TERMINATE,
                "step": {
                    "name": "Marketing Head Review",
                    "required_role": roles["marketing_head"],
                    "scope_resolution_policy": ScopeResolutionPolicy.ANCESTOR_OF_TYPE,
                    "ancestor_node_type": NodeType.COMPANY.value,
                    "display_order": 1,
                },
            },
            {
                "name": "HO Head Approval",
                "display_order": 3,
                "parallel_mode": ParallelMode.SINGLE,
                "on_rejection_action": RejectionAction.TERMINATE,
                "step": {
                    "name": "HO Head Review",
                    "required_role": roles["ho_head"],
                    "scope_resolution_policy": ScopeResolutionPolicy.ANCESTOR_OF_TYPE,
                    "ancestor_node_type": NodeType.COMPANY.value,
                    "display_order": 1,
                },
            },
        ]
        self._create_groups_and_steps(version, groups_def, "campaign")

    def _create_groups_and_steps(self, version, groups_def, label):
        for g_def in groups_def:
            step_def = g_def.pop("step")
            group, g_created = StepGroup.objects.get_or_create(
                template_version=version,
                display_order=g_def["display_order"],
                defaults=g_def,
            )
            self.stdout.write(
                f"  {'Created' if g_created else 'Exists '} [{label}] group: {group.name}"
            )
            step, s_created = WorkflowStep.objects.get_or_create(
                group=group,
                name=step_def["name"],
                defaults=step_def,
            )
            self.stdout.write(
                f"    {'Created' if s_created else 'Exists '} step: {step.name}"
            )

    # ── Budgets ───────────────────────────────────────────────────────────────

    def _budgets(self, org, company_a, entity_x, entity_y, users):
        admin = users["tenantadmin@demo.local"]

        mkt_cat, created = BudgetCategory.objects.get_or_create(
            org=org,
            code="marketing",
            defaults={"name": "Marketing", "is_active": True},
        )
        self.stdout.write(f"  {'Created' if created else 'Exists '} category: Marketing")

        for code, name in [
            ("digital-ads", "Digital Ads"),
            ("events", "Events"),
            ("influencer", "Influencer"),
        ]:
            _, created = BudgetSubCategory.objects.get_or_create(
                category=mkt_cat,
                code=code,
                defaults={"name": name, "is_active": True},
            )
            self.stdout.write(f"  {'Created' if created else 'Exists '} subcategory: {name}")

        digital_ads = BudgetSubCategory.objects.get(category=mkt_cat, code="digital-ads")

        budget_specs = [
            (entity_x, "Entity X", Decimal("50000000.00")),
            (entity_y, "Entity Y", Decimal("50000000.00")),
            (company_a, "Company A", Decimal("100000000.00")),
        ]
        for node, label, amount in budget_specs:
            budget, created = Budget.objects.get_or_create(
                org=org,
                scope_node=node,
                category=mkt_cat,
                subcategory=digital_ads,
                financial_year="2026-27",
                period_type=PeriodType.YEARLY,
                period_start="2026-04-01",
                period_end="2027-03-31",
                defaults={
                    "allocated_amount": amount,
                    "reserved_amount": Decimal("0"),
                    "consumed_amount": Decimal("0"),
                    "currency": "INR",
                    "status": BudgetStatus.ACTIVE,
                    "created_by": admin,
                },
            )
            self.stdout.write(
                f"  {'Created' if created else 'Exists '} budget: {label} / Marketing / Digital Ads 2026-27"
            )
            BudgetRule.objects.get_or_create(
                budget=budget,
                defaults={
                    "warning_threshold_percent": Decimal("80.00"),
                    "approval_threshold_percent": Decimal("100.00"),
                    "hard_block_threshold_percent": Decimal("110.00"),
                    "allowed_variance_percent": Decimal("10.00"),
                    "require_hod_approval_on_variance": True,
                },
            )

        return mkt_cat, digital_ads

    # ── Demo Campaign ─────────────────────────────────────────────────────────

    def _demo_campaign(self, org, entity_x, mkt_cat, digital_ads, users):
        admin = users["tenantadmin@demo.local"]
        budget_x = Budget.objects.filter(
            org=org,
            scope_node=entity_x,
            category=mkt_cat,
            subcategory=digital_ads,
            financial_year="2026-27",
        ).first()

        campaign, created = Campaign.objects.get_or_create(
            org=org,
            scope_node=entity_x,
            code="demo-digital-q1",
            defaults={
                "name": "Demo Digital Ads Q1",
                "description": "Quarterly digital advertising spend for Entity X",
                "campaign_type": "digital",
                "start_date": "2026-04-01",
                "end_date": "2026-06-30",
                "requested_amount": Decimal("1000000.00"),
                "currency": "INR",
                "category": mkt_cat,
                "subcategory": digital_ads,
                "budget": budget_x,
                "created_by": admin,
                "status": CampaignStatus.DRAFT,
            },
        )
        self.stdout.write(f"  {'Created' if created else 'Exists '} demo campaign: {campaign.name}")

    # ── Demo Invoice ──────────────────────────────────────────────────────────

    def _demo_invoice(self, entity_x, users):
        creator = users["entitymanager.x@demo.local"]
        invoice, created = Invoice.objects.get_or_create(
            title="Demo Invoice - Acme Corp Services",
            scope_node=entity_x,
            defaults={
                "amount": Decimal("50000.00"),
                "currency": "INR",
                "created_by": creator,
                "status": InvoiceStatus.DRAFT,
            },
        )
        self.stdout.write(f"  {'Created' if created else 'Exists '} demo invoice: {invoice.title}")

    # ── Vendor Seed ───────────────────────────────────────────────────────────

    def _vendors(self, org, entity_x, users):
        admin = users["tenantadmin@demo.local"]

        # (a) Pending invitation — primary UAT onboarding entry point
        pending_email = "pending-vendor@acmecorp.example.com"
        _, created = VendorInvitation.objects.get_or_create(
            org=org,
            scope_node=entity_x,
            vendor_email=pending_email,
            defaults={
                "invited_by": admin,
                "vendor_name_hint": "Acme Corp",
                "token": secrets.token_urlsafe(48),
                "status": InvitationStatus.PENDING,
            },
        )
        self.stdout.write(
            f"  {'Created' if created else 'Exists '} pending vendor invitation: {pending_email}"
        )

        # (b) Submitted -> waiting for send-to-finance (finance flow test)
        finance_email = "finance-pending-vendor@example.com"
        finance_inv, fi_created = VendorInvitation.objects.get_or_create(
            org=org,
            scope_node=entity_x,
            vendor_email=finance_email,
            defaults={
                "invited_by": admin,
                "vendor_name_hint": "Finance Pending Co",
                "token": secrets.token_urlsafe(48),
                "status": InvitationStatus.SUBMITTED,
            },
        )
        VendorOnboardingSubmission.objects.get_or_create(
            invitation=finance_inv,
            defaults={
                "status": SubmissionStatus.SUBMITTED,
                "submission_mode": "manual",
                "normalized_vendor_name": "Finance Pending Co",
                "normalized_email": finance_email,
                "normalized_bank_name": "Axis Bank",
                "normalized_account_number": "1234567890",
                "normalized_ifsc": "UTIB0001234",
                "raw_form_data": {
                    "vendor_name": "Finance Pending Co",
                    "email": finance_email,
                },
            },
        )
        self.stdout.write(
            f"  {'Created' if fi_created else 'Exists '} finance-pending vendor: {finance_email}"
        )

        # (c) Fully activated vendor — convenience record for invoice tests
        active_name = "Demo Supplies Ltd"
        active_inv, _ = VendorInvitation.objects.get_or_create(
            org=org,
            scope_node=entity_x,
            vendor_email="demo-supplies@example.com",
            defaults={
                "invited_by": admin,
                "vendor_name_hint": active_name,
                "token": secrets.token_urlsafe(48),
                "status": InvitationStatus.SUBMITTED,
            },
        )
        active_sub, sub_created = VendorOnboardingSubmission.objects.get_or_create(
            invitation=active_inv,
            defaults={
                "status": SubmissionStatus.ACTIVATED,
                "submission_mode": "manual",
                "normalized_vendor_name": active_name,
                "normalized_vendor_type": "supplier",
                "normalized_email": "demo-supplies@example.com",
                "normalized_phone": "9000001111",
                "normalized_gstin": "22AABCD1234E1Z5",
                "normalized_pan": "AABCD1234E",
                "normalized_address_line1": "456 Supply Road",
                "normalized_city": "Mumbai",
                "normalized_state": "Maharashtra",
                "normalized_country": "India",
                "normalized_pincode": "400002",
                "normalized_bank_name": "HDFC Bank",
                "normalized_account_number": "9876543210",
                "normalized_ifsc": "HDFC0001234",
                "finance_vendor_code": "SAP-DEMO-001",
                "raw_form_data": {
                    "vendor_name": active_name,
                    "email": "demo-supplies@example.com",
                },
            },
        )
        active_vendor, av_created = Vendor.objects.get_or_create(
            onboarding_submission=active_sub,
            defaults={
                "org": org,
                "scope_node": entity_x,
                "vendor_name": active_name,
                "email": "demo-supplies@example.com",
                "phone": "9000001111",
                "sap_vendor_id": "SAP-DEMO-001",
                "po_mandate_enabled": True,
                "marketing_status": MarketingStatus.APPROVED,
                "operational_status": OperationalStatus.ACTIVE,
                "approved_by_marketing": admin,
                "approved_at": timezone.now(),
            },
        )
        self.stdout.write(
            f"  {'Created' if av_created else 'Exists '} active vendor: {active_name} "
            f"(SAP-DEMO-001, PO mandate ON)"
        )

    # ── UAT Summary ───────────────────────────────────────────────────────────

    def _print_summary(self, org, company_a, entity_x, entity_y):
        w = self.stdout.write
        S = self.style.SUCCESS
        H = self.style.MIGRATE_HEADING

        w("")
        w(S("=" * 65))
        w(S("  UAT Demo Seed Complete"))
        w(S("=" * 65))
        w("")
        w(H("Organization:"))
        w(f"  {org.name}  (code: {org.code})")
        w("")
        w(H("Scope Hierarchy:"))
        w(f"  {org.name}")
        w(f"  +-- {company_a.name}  [{company_a.node_type}]")
        w(f"       +-- {entity_x.name}  [{entity_x.node_type}]")
        w(f"       +-- {entity_y.name}  [{entity_y.node_type}]")
        w("")
        w(H("Internal Users  (all password: Password@123)"))
        rows = [
            ("tenantadmin@demo.local", "Org Admin (full access)", "Company A"),
            ("entitymanager.x@demo.local", "Entity Manager", "Entity X  -> invoice step 1 for X"),
            ("entitymanager.y@demo.local", "Entity Manager", "Entity Y  -> invoice step 1 for Y"),
            ("marketinghead@demo.local", "Marketing Head", "Company A -> invoice step 2"),
            ("ho@demo.local", "HO Head", "Company A -> invoice step 3"),
            ("finance@demo.local", "Finance Team", "Company A -> invoice step 4 + external handoff target"),
        ]
        for email, role, scope in rows:
            w(f"  {email:<38}  {role:<22}  @ {scope}")
        w("")
        w(H("Invoice Workflow Steps (4-step, hierarchy-ascending):"))
        w("  1  entity_manager    SUBJECT_NODE              -> entity-x manager / entity-y manager")
        w("  2  marketing_head    ANCESTOR_OF_TYPE(company) -> Company A")
        w("  3  ho_head           ANCESTOR_OF_TYPE(company) -> Company A")
        w("  4  finance_team      ORG_ROOT                  -> Company A")
        w("     [after step 4 internally approved -> FinanceHandoff auto-fires -> email to finance@demo.local]")
        w("")
        w(H("Vendor Seed:"))
        w("  [PENDING]   pending-vendor@acmecorp.example.com  — invitation token ready, primary UAT path")
        w("  [SUBMITTED] finance-pending-vendor@example.com   — submitted, ready for send-to-finance test")
        w("  [ACTIVE]    Demo Supplies Ltd  (SAP-DEMO-001)     — convenience vendor, PO mandate ON")
        w("")
        w(H("Finance Email Configuration:"))
        w("  Set in .env:  VENDOR_FINANCE_RECIPIENTS=finance@demo.local")
        w("  Token URLs are generated at send-to-finance time (vendor) or workflow completion (invoice/campaign)")
        w("  Public token endpoint (no auth):  GET/POST /api/v1/finance/tokens/<token>/")
        w("")
        w(H("Full UAT Test Flow:"))
        w("")
        w("  Vendor Onboarding:")
        w("    1. tenantadmin invites vendor    POST /api/v1/vendors/invitations/")
        w("    2. vendor opens form via token   GET  /api/v1/vendors/public/invitations/{token}/")
        w("    3. vendor submits VRF            POST /api/v1/vendors/public/invitations/{token}/submit-manual/")
        w("    4. admin sends to finance        POST /api/v1/vendors/submissions/{id}/send-to-finance/")
        w("    5. finance clicks email link to approve/reject  (token URL, no auth required)")
        w("    6. on approve: finance enters vendor ID (SAP code)")
        w("    7. admin/marketing approves      POST /api/v1/vendors/{id}/marketing-approve/")
        w("    8. vendor becomes ACTIVE with PO mandate configurable")
        w("")
        w("  Invoice Approval Flow (Entity X example):")
        w("    1. entitymanager.x creates invoice    POST /api/v1/invoices/  {scope_node: entity-x}")
        w("    2. start workflow                     POST /api/v1/invoices/{id}/start-workflow/")
        w("    3. entitymanager.x approves  (step 1: SUBJECT_NODE -> entity-x)")
        w("    4. marketinghead approves    (step 2: ANCESTOR_OF_TYPE company -> company-a)")
        w("    5. ho approves               (step 3: ANCESTOR_OF_TYPE company -> company-a)")
        w("    6. finance approves          (step 4: ORG_ROOT -> company-a)")
        w("       -> invoice.status: internally_approved")
        w("       -> FinanceHandoff auto-created + email sent to VENDOR_FINANCE_RECIPIENTS")
        w("    7. external finance clicks approve/reject link  (public token endpoint)")
        w("       -> invoice.status: finance_approved  OR  finance_rejected")
        w("")
        w("  Same flow works for Entity Y with entitymanager.y as step-1 approver.")
        w("")
        w(S("=" * 65))
