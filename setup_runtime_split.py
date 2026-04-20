"""
Quick script to set up fresh workflow templates for runtime split UAT.
Paste this entire script into: python manage.py shell

Usage:
  1. python manage.py shell
  2. paste entire contents of this file
  3. press Enter
"""
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
django.setup()

from django.contrib.auth import get_user_model
from apps.workflow.models import (
    WorkflowTemplate, WorkflowTemplateVersion, StepGroup, WorkflowStep,
    WorkflowSplitOption, StepKind, VersionStatus, GroupStatus, StepStatus,
    AssignmentState, AllocationTotalPolicy,
)
from apps.core.models import ScopeNode
from apps.access.models import Role

User = get_user_model()

# ── Config ──────────────────────────────────────────────────────────────────
ORG_CODE = "DEMO"
MODULE   = "invoice"

# Two entities we want to split across
ENTITY_1_CODE = "MKTG"
ENTITY_2_CODE = "SALES"

# Role codes (must exist in DB)
MARKETING_HEAD_CODE = "MARKETING_HEAD"
MARKETING_EXEC_CODE = "MARKETING_EXEC"
HO_OPS_CODE         = "HO_OPS"
HO_HEAD_CODE        = "HO_HEAD"

# Step display names
G1 = "Group 1 — Marketing Approval"
G2 = "Group 2 — HO Operations"
G3 = "Group 3 — HO Head"

# ── Resolve existing records ──────────────────────────────────────────────────

org = ScopeNode.objects.filter(code=ORG_CODE, parent__isnull=True).first()
if not org:
    raise SystemExit(f"Org with code '{ORG_CODE}' not found. Create it first.")

root = org  # all children are under this org node

def get_child(code):
    node = ScopeNode.objects.filter(org=org, code=code).first()
    if not node:
        raise SystemExit(f"ScopeNode '{code}' not found under org '{ORG_CODE}'.")
    return node

entity_mktg = get_child(ENTITY_1_CODE)
entity_sales = get_child(ENTITY_2_CODE)

def get_role(code):
    role = Role.objects.filter(code=code, org=org).first()
    if not role:
        raise SystemExit(f"Role '{code}' not found in org '{ORG_CODE}'.")
    return role

mktg_head = get_role(MARKETING_HEAD_CODE)
mktg_exec = get_role(MARKETING_EXEC_CODE)
ho_ops    = get_role(HO_OPS_CODE)
ho_head   = get_role(HO_HEAD_CODE)

# Get a superuser for created_by / published_by
admin_user = User.objects.filter(is_superuser=True).first()
if not admin_user:
    raise SystemExit("No superuser found. Create one first.")

# ── Clean existing demo templates ──────────────────────────────────────────

deleted = WorkflowTemplate.objects.filter(
    scope_node=root, module=MODULE
).delete()
print(f"Deleted {deleted[0]} existing templates.")

# ── TEMPLATE A: Normal (4 steps) ────────────────────────────────────────────

print("\n=== Creating Normal Approval Template ===")

tmpl_a = WorkflowTemplate.objects.create(
    name="Invoice — Normal Approval",
    module=MODULE,
    scope_node=root,
    created_by=admin_user,
)

v1_a = WorkflowTemplateVersion.objects.create(
    template=tmpl_a,
    version_number=1,
    status=VersionStatus.PUBLISHED,
    published_by=admin_user,
)

# Group 1: Marketing Approval
g1_a = StepGroup.objects.create(
    template_version=v1_a,
    name=G1,
    display_order=0,
    parallel_mode="ALL_MUST_COMPLETE",
    on_rejection_action="GO_TO_GROUP",
)
# Steps inside Group 1
WorkflowStep.objects.create(
    group=g1_a, name="Marketing Approval", required_role=mktg_head,
    step_kind=StepKind.NORMAL_APPROVAL, display_order=0,
)
WorkflowStep.objects.create(
    group=g1_a, name="Marketing Exec Check", required_role=mktg_exec,
    step_kind=StepKind.NORMAL_APPROVAL, display_order=1,
)

# Group 2: HO Ops
g2_a = StepGroup.objects.create(
    template_version=v1_a,
    name=G2,
    display_order=1,
    parallel_mode="ALL_MUST_COMPLETE",
    on_rejection_action="GO_TO_GROUP",
)
WorkflowStep.objects.create(
    group=g2_a, name="HO Ops Approval", required_role=ho_ops,
    step_kind=StepKind.NORMAL_APPROVAL, display_order=0,
)

# Group 3: HO Head
g3_a = StepGroup.objects.create(
    template_version=v1_a,
    name=G3,
    display_order=2,
    parallel_mode="ALL_MUST_COMPLETE",
    on_rejection_action="GO_TO_GROUP",
)
WorkflowStep.objects.create(
    group=g3_a, name="HO Head Approval", required_role=ho_head,
    step_kind=StepKind.NORMAL_APPROVAL, display_order=0,
)

print(f"  Template: {tmpl_a.name} (published)")

# ── TEMPLATE B: Runtime Split (with RUNTIME_SPLIT_ALLOCATION) ───────────────

print("\n=== Creating Runtime Split Template ===")

tmpl_b = WorkflowTemplate.objects.create(
    name="Invoice — Runtime Split",
    module=MODULE,
    scope_node=root,
    created_by=admin_user,
)

v1_b = WorkflowTemplateVersion.objects.create(
    template=tmpl_b,
    version_number=1,
    status=VersionStatus.PUBLISHED,
    published_by=admin_user,
)

# Group 1: Marketing Head splits invoice → branches go to HO Ops
g1_b = StepGroup.objects.create(
    template_version=v1_b,
    name=G1,
    display_order=0,
    parallel_mode="ALL_MUST_COMPLETE",
    on_rejection_action="BRANCH_CORRECTION",
)

split_step = WorkflowStep.objects.create(
    group=g1_b,
    name="Marketing Head Split",
    required_role=mktg_head,
    step_kind=StepKind.RUNTIME_SPLIT_ALLOCATION,
    display_order=0,
    scope_resolution_policy="SUBJECT_NODE",
    allocation_total_policy=AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL,
    approver_selection_mode="POOL",
    require_category=False,
    require_subcategory=False,
    require_budget=False,
    require_campaign=False,
    allow_multiple_lines_per_entity=False,
)

# Group 2: Each branch is approved by HO Ops for that entity
g2_b = StepGroup.objects.create(
    template_version=v1_b,
    name=G2,
    display_order=1,
    parallel_mode="ALL_MUST_COMPLETE",
    on_rejection_action="GO_TO_GROUP",
)
WorkflowStep.objects.create(
    group=g2_b, name="HO Ops Branch Approval", required_role=ho_ops,
    step_kind=StepKind.NORMAL_APPROVAL, display_order=0,
)

# Group 3: HO Head
g3_b = StepGroup.objects.create(
    template_version=v1_b,
    name=G3,
    display_order=2,
    parallel_mode="ALL_MUST_COMPLETE",
    on_rejection_action="GO_TO_GROUP",
)
WorkflowStep.objects.create(
    group=g3_b, name="HO Head Final Approval", required_role=ho_head,
    step_kind=StepKind.NORMAL_APPROVAL, display_order=0,
)

print(f"  Template: {tmpl_b.name} (published)")

# ── Seed WorkflowSplitOption ──────────────────────────────────────────────────

print("\n=== Creating Split Options (allowed entities + approvers) ===")

# Clear any existing split options for this step
WorkflowSplitOption.objects.filter(workflow_step=split_step).delete()

WorkflowSplitOption.objects.create(
    workflow_step=split_step,
    entity=entity_mktg,
    approver_role=ho_ops,       # HO Ops approves the MKTG branch
    is_active=True,
    display_order=0,
)
WorkflowSplitOption.objects.create(
    workflow_step=split_step,
    entity=entity_sales,
    approver_role=ho_ops,       # HO Ops also approves SALES branch
    is_active=True,
    display_order=1,
)

print(f"  Split options: MKTG entity → {ho_ops}, SALES entity → {ho_ops}")
print(f"  Split step ID for reference: {split_step.id}")

# ── Summary ─────────────────────────────────────────────────────────────────

print("\n=== Done ===")
print(f"  Org: {org.code} ({org.name})")
print(f"  Entity 1: {entity_mktg.code} ({entity_mktg.name})")
print(f"  Entity 2: {entity_sales.code} ({entity_sales.name})")
print(f"  Roles: {mktg_head.code} | {mktg_exec.code} | {ho_ops.code} | {ho_head.code}")
print(f"  Normal template ID : {tmpl_a.id}")
print(f"  Runtime Split template ID: {tmpl_b.id}")
print(f"  SplitStep ID        : {split_step.id}")
