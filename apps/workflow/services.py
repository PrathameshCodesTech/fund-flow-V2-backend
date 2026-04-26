from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.models import ScopeNode
from apps.core.services import get_ancestors
from apps.access.selectors import get_users_with_role_at_node
from apps.workflow.models import (
    AssignmentState,
    ScopeResolutionPolicy,
    VersionStatus,
    InstanceStatus,
    GroupStatus,
    StepStatus,
    ParallelMode,
    RejectionAction,
    WorkflowEventType,
    WorkflowTemplate,
    WorkflowTemplateVersion,
    WorkflowStep,
    WorkflowInstance,
    WorkflowInstanceGroup,
    WorkflowInstanceStep,
    WorkflowInstanceBranch,
    WorkflowEvent,
    StepKind,
    BranchStatus,
)
from apps.notifications.models import (
    NotificationDelivery,
    NotificationChannel,
    NotificationStatus,
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ModuleInactiveError(ValueError):
    """Raised when a module is inactive for the subject scope node."""


class WorkflowNotConfiguredError(ValueError):
    """Raised when no published template is found in the walk-up chain."""


class StepActionError(ValueError):
    """Raised when a step action is invalid (wrong actor, wrong state, etc.)."""


class AllocationCoverageError(ValueError):
    """
    Raised when a workflow instance cannot complete internal approval because
    the invoice's InvoiceAllocation rows do not fully cover the invoice amount.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _has_allocation_capable_step(instance) -> bool:
    """True when the workflow template version contains at least one allocation step."""
    return WorkflowStep.objects.filter(
        group__template_version=instance.template_version,
        step_kind__in=[StepKind.SINGLE_ALLOCATION, StepKind.RUNTIME_SPLIT_ALLOCATION],
    ).exists()


def _check_allocation_coverage_or_raise(instance):
    """
    For invoice subjects whose workflow has an allocation-capable step, validate
    that InvoiceAllocation rows sum exactly to invoice.amount.

    Active statuses that count toward coverage:
        SUBMITTED       — single allocation submitted by actor (budget reserved)
        BRANCH_PENDING  — runtime split branch awaiting approver (budget reserved)
        APPROVED        — approved by branch approver or auto-approved

    Cancelled, rejected, draft, and correction-required rows are excluded.

    Raises AllocationCoverageError if coverage is incomplete.
    Silently passes if the workflow has no allocation-capable steps (non-invasive).
    """
    if not _has_allocation_capable_step(instance):
        return

    from apps.invoices.models import Invoice, InvoiceAllocation, InvoiceAllocationStatus
    from django.db.models import Sum

    try:
        invoice = Invoice.objects.get(pk=instance.subject_id)
    except Invoice.DoesNotExist:
        return  # Can't validate without invoice record — let the handoff layer catch it

    active_statuses = (
        InvoiceAllocationStatus.SUBMITTED,
        InvoiceAllocationStatus.BRANCH_PENDING,
        InvoiceAllocationStatus.APPROVED,
    )
    coverage = (
        InvoiceAllocation.objects.filter(
            invoice=invoice,
            workflow_instance=instance,
            status__in=active_statuses,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )

    if coverage != invoice.amount:
        gap = invoice.amount - coverage
        raise AllocationCoverageError(
            f"Invoice {invoice.id} allocation coverage is incomplete: "
            f"{coverage} allocated of {invoice.amount} required "
            f"(gap: {gap}). Submit full allocation before approving."
        )


def _release_allocation_budgets(instance, actor, note=""):
    """
    Release any outstanding reserved budget for every InvoiceAllocation tied to
    this workflow instance.

    Uses line-level resolution (resolve_budget_line_for_allocation + release_reserved_budget_line)
    for allocations created after the BudgetLine refactor. Falls back to header-level release
    only for legacy allocations where the original BudgetLine cannot be determined from
    (category_id, subcategory_id) context.

    No-op for non-invoice subjects or allocations with no budget.
    Failures are swallowed so a budget accounting problem never blocks workflow state.
    """
    if instance.subject_type != "invoice":
        return

    from apps.invoices.models import InvoiceAllocation
    from apps.budgets.models import SourceType
    from apps.budgets.services import release_reserved_budget, release_reserved_budget_line
    from apps.budgets.services import resolve_budget_line_for_allocation, get_source_reserved_balance_for_line, BudgetLineNotFoundError

    for alloc in InvoiceAllocation.objects.filter(
        workflow_instance=instance,
    ).select_related("budget", "category", "subcategory"):
        if not alloc.budget_id:
            continue
        source_id = f"invoice:{alloc.invoice_id}:allocation:{alloc.id}"

        # Try line-level release first (new allocation path)
        try:
            budget_line = resolve_budget_line_for_allocation(
                budget=alloc.budget,
                category_id=alloc.category_id,
                subcategory_id=alloc.subcategory_id,
            )
            balance = get_source_reserved_balance_for_line(
                budget_line, SourceType.INVOICE, source_id
            )
            if balance <= Decimal("0"):
                continue
            release_reserved_budget_line(
                line=budget_line,
                amount=balance,
                source_type=SourceType.INVOICE,
                source_id=source_id,
                released_by=actor,
                note=note or f"Workflow instance {instance.id} terminated — budget released",
            )
            _emit_event(
                instance,
                WorkflowEventType.ALLOCATION_BUDGET_RELEASED,
                actor,
                metadata={
                    "allocation_id": alloc.id,
                    "budget_id": alloc.budget_id,
                    "budget_line_id": budget_line.id,
                    "released_amount": str(balance),
                },
            )
        except BudgetLineNotFoundError:
            # Legacy allocation without matching BudgetLine — fall back to header release
            try:
                from apps.budgets.services import get_source_reserved_balance
                balance = get_source_reserved_balance(alloc.budget, SourceType.INVOICE, source_id)
                if balance <= Decimal("0"):
                    continue
                release_reserved_budget(
                    budget=alloc.budget,
                    amount=balance,
                    source_type=SourceType.INVOICE,
                    source_id=source_id,
                    released_by=actor,
                    note=note or f"Workflow instance {instance.id} terminated — budget released (legacy)",
                )
                _emit_event(
                    instance,
                    WorkflowEventType.ALLOCATION_BUDGET_RELEASED,
                    actor,
                    metadata={
                        "allocation_id": alloc.id,
                        "budget_id": alloc.budget_id,
                        "released_amount": str(balance),
                        "legacy": True,
                    },
                )
            except Exception:
                pass  # Non-fatal; ops can reconcile manually
        except Exception:
            pass  # Non-fatal; ops can reconcile manually


def _consume_allocation_budgets(instance, actor):
    """
    Consume reserved budget for all approved allocations when instance is finally approved.

    Uses line-level resolution (resolve_budget_line_for_allocation + consume_reserved_budget_line)
    for allocations created after the BudgetLine refactor. Falls back to header-level consume
    only for legacy allocations where the original BudgetLine cannot be determined from
    (category_id, subcategory_id) context.
    """
    from apps.invoices.models import InvoiceAllocation, InvoiceAllocationStatus
    from apps.budgets.models import SourceType
    from apps.budgets.services import consume_reserved_budget, consume_reserved_budget_line
    from apps.budgets.services import resolve_budget_line_for_allocation, BudgetLineNotFoundError
    from apps.workflow.models import WorkflowEventType

    for alloc in InvoiceAllocation.objects.filter(
        workflow_instance=instance,
        status=InvoiceAllocationStatus.APPROVED,
    ).select_related("budget", "category", "subcategory"):
        if not alloc.budget_id:
            continue
        source_id = f"invoice:{alloc.invoice_id}:allocation:{alloc.id}"
        legacy_fallback = False

        # Try line-level consume first for new allocation data.
        try:
            budget_line = resolve_budget_line_for_allocation(
                budget=alloc.budget,
                category_id=alloc.category_id,
                subcategory_id=alloc.subcategory_id,
            )
            from apps.budgets.services import get_source_reserved_balance_for_line
            balance = get_source_reserved_balance_for_line(
                budget_line, SourceType.INVOICE, source_id
            )
            if balance > Decimal("0"):
                consume_reserved_budget_line(
                    line=budget_line,
                    amount=balance,
                    source_type=SourceType.INVOICE,
                    source_id=source_id,
                    consumed_by=actor,
                    note=f"Final invoice approval — allocation {alloc.id}",
                )
                consumed_line_level = True
                _emit_event(
                    instance,
                    WorkflowEventType.ALLOCATION_BUDGET_CONSUMED,
                    actor,
                    metadata={
                        "allocation_id": alloc.id,
                        "budget_id": alloc.budget_id,
                        "budget_line_id": budget_line.id,
                    },
                )
            continue
        except BudgetLineNotFoundError:
            # Legacy allocation without matching BudgetLine — fall back to header consume.
            legacy_fallback = True
        except Exception as e:
            # New data should not silently fall back to header-level accounting.
            alloc.metadata = {**(alloc.metadata or {}), "budget_consume_error": str(e)}
            alloc.save(update_fields=["metadata"])
            _emit_event(
                instance,
                WorkflowEventType.ALLOCATION_BUDGET_RELEASED,  # reuse event for failure signal
                actor,
                metadata={
                    "allocation_id": alloc.id,
                    "budget_id": alloc.budget_id,
                    "consume_failed": True,
                    "error": str(e),
                },
            )
            continue

        if legacy_fallback:
            try:
                consume_reserved_budget(
                    budget=alloc.budget,
                    source_type=SourceType.INVOICE,
                    source_id=source_id,
                    consumed_by=actor,
                    note=f"Final invoice approval — allocation {alloc.id} (legacy)",
                )
                _emit_event(
                    instance,
                    WorkflowEventType.ALLOCATION_BUDGET_CONSUMED,
                    actor,
                    metadata={"allocation_id": alloc.id, "budget_id": alloc.budget_id, "legacy": True},
                )
            except Exception as e:
                # Record failure in allocation metadata for finance visibility
                alloc.metadata = {**(alloc.metadata or {}), "budget_consume_error": str(e)}
                alloc.save(update_fields=["metadata"])
                _emit_event(
                    instance,
                    WorkflowEventType.ALLOCATION_BUDGET_RELEASED,  # reuse event for failure signal
                    actor,
                    metadata={
                        "allocation_id": alloc.id,
                        "budget_id": alloc.budget_id,
                        "consume_failed": True,
                        "error": str(e),
                        "legacy": True,
                    },
                )


def _reverse_consumed_allocation_budgets(instance, actor, note=""):
    """
    Reverse consumed budget amounts for invoice allocations.

    Used as a compatibility repair path when finance rejects an invoice that
    had already been consumed by the older internal-approval logic.
    """
    if instance.subject_type != "invoice":
        return

    from apps.invoices.models import InvoiceAllocation, InvoiceAllocationStatus
    from apps.budgets.models import (
        SourceType,
        BudgetConsumption,
        ConsumptionType,
        ConsumptionStatus,
    )
    from apps.budgets.services import resolve_budget_line_for_allocation, BudgetLineNotFoundError
    from django.db.models import Sum

    for alloc in InvoiceAllocation.objects.filter(
        workflow_instance=instance,
        status=InvoiceAllocationStatus.APPROVED,
    ).select_related("budget", "category", "subcategory"):
        if not alloc.budget_id:
            continue

        source_id = f"invoice:{alloc.invoice_id}:allocation:{alloc.id}"
        try:
            budget_line = resolve_budget_line_for_allocation(
                budget=alloc.budget,
                category_id=alloc.category_id,
                subcategory_id=alloc.subcategory_id,
            )
            consumed = (
                BudgetConsumption.objects.filter(
                    budget_line=budget_line,
                    source_type=SourceType.INVOICE,
                    source_id=source_id,
                    consumption_type=ConsumptionType.CONSUMED,
                    status=ConsumptionStatus.APPLIED,
                ).aggregate(t=Sum("amount"))["t"] or Decimal("0")
            )
            if consumed > Decimal("0"):
                budget_line.consumed_amount -= consumed
                budget_line.save(update_fields=["consumed_amount", "updated_at"])
                alloc.budget.consumed_amount -= consumed
                alloc.budget.save(update_fields=["consumed_amount", "updated_at"])
                BudgetConsumption.objects.create(
                    budget=alloc.budget,
                    budget_line=budget_line,
                    source_type=SourceType.INVOICE,
                    source_id=source_id,
                    amount=consumed,
                    consumption_type=ConsumptionType.ADJUSTED,
                    status=ConsumptionStatus.APPLIED,
                    created_by=actor,
                    note=note or f"Finance rejection reversal for allocation {alloc.id}",
                )
                _emit_event(
                    instance,
                    WorkflowEventType.ALLOCATION_BUDGET_RELEASED,
                    actor,
                    metadata={
                        "allocation_id": alloc.id,
                        "budget_id": alloc.budget_id,
                        "budget_line_id": budget_line.id,
                        "reversed_consumed_amount": str(consumed),
                    },
                )
        except BudgetLineNotFoundError:
            continue
        except Exception as e:
            alloc.metadata = {**(alloc.metadata or {}), "budget_reverse_error": str(e)}
            alloc.save(update_fields=["metadata"])


def _emit_event(instance, event_type, actor_user, target_user=None, metadata=None):
    """Create a WorkflowEvent and an in_app NotificationDelivery in one call."""
    event = WorkflowEvent.objects.create(
        instance=instance,
        event_type=event_type,
        actor_user=actor_user,
        target_user=target_user,
        metadata=metadata or {},
    )
    if target_user is not None:
        NotificationDelivery.objects.create(
            event=event,
            channel=NotificationChannel.IN_APP,
            status=NotificationStatus.PENDING,
        )
    return event


def _assert_workflow_subject_vendor_not_on_hold(instance) -> None:
    """
    If the workflow instance's subject is an invoice with a vendor on hold,
    raise VendorProfileHoldError to freeze step/branch approvals.
    """
    if instance.subject_type != "invoice":
        return
    from apps.invoices.models import Invoice
    from apps.vendors.models import Vendor
    from apps.vendors.services import assert_vendor_profile_not_on_hold
    try:
        vendor_id = Invoice.objects.filter(pk=instance.subject_id).values_list("vendor_id", flat=True).first()
        if vendor_id:
            vendor = Vendor.objects.only("profile_change_pending", "profile_hold_reason").get(pk=vendor_id)
            assert_vendor_profile_not_on_hold(vendor)
    except (Invoice.DoesNotExist, Vendor.DoesNotExist):
        pass


def _assert_step_actionable(instance_step):
    """Raise StepActionError if the step cannot be acted upon right now."""
    group = instance_step.instance_group
    if group.status != GroupStatus.IN_PROGRESS:
        raise StepActionError(
            f"Step {instance_step.id} is not actionable: "
            f"its group status is '{group.status}', expected IN_PROGRESS."
        )
    if instance_step.status not in (StepStatus.WAITING, StepStatus.WAITING_BRANCHES):
        raise StepActionError(
            f"Step {instance_step.id} is not actionable: "
            f"its status is '{instance_step.status}', expected WAITING or WAITING_BRANCHES."
        )
    if instance_step.status == StepStatus.WAITING_BRANCHES:
        if instance_step.workflow_step.step_kind != StepKind.RUNTIME_SPLIT_ALLOCATION:
            raise StepActionError(
                f"Step {instance_step.id} is a SPLIT_BY_SCOPE step and cannot be "
                f"acted on directly — use branch approve/reject endpoints."
            )
    if not instance_step.assigned_user_id:
        raise StepActionError(f"Step {instance_step.id} has no assigned user.")


def _advance_on_group_complete(group, instance, acted_by):
    """
    Mark group APPROVED and either advance to the next group or close the instance.

    For SPLIT_BY_SCOPE steps within the next group: automatically triggers split
    activation (branch creation) before emitting STEP_ASSIGNED events.

    Called after the group's parallel_mode completion condition is met.
    """
    group.status = GroupStatus.APPROVED
    group.save(update_fields=["status"])

    next_group = (
        instance.instance_groups
        .filter(display_order__gt=group.display_order)
        .order_by("display_order")
        .first()
    )

    if next_group:
        next_group.status = GroupStatus.IN_PROGRESS
        next_group.save(update_fields=["status"])
        instance.current_group = next_group
        instance.save(update_fields=["current_group"])

        _activate_group_entry_steps(next_group, instance, acted_by)
    else:
        # Allocation coverage gate: invoice workflows with an allocation step must
        # have full coverage before the instance can reach APPROVED.
        if instance.subject_type == "invoice":
            _check_allocation_coverage_or_raise(instance)

        instance.status = InstanceStatus.APPROVED
        instance.completed_at = timezone.now()
        instance.save(update_fields=["status", "completed_at"])
        _emit_event(instance, WorkflowEventType.INSTANCE_APPROVED, acted_by)
        _sync_subject_status_on_workflow_change(instance)


def _activate_group_entry_steps(group, instance, actor_user):
    """
    Emit assignment events for normal steps and start split steps as soon as a
    group becomes active. Split parent steps are not human tasks; they fan out
    into branch tasks immediately.
    """
    for ist in group.instance_steps.select_related("workflow_step", "assigned_user").order_by(
        "workflow_step__display_order"
    ):
        if ist.workflow_step.step_kind == StepKind.SPLIT_BY_SCOPE:
            split_instance_step(ist)
            continue

        # RUNTIME_SPLIT_ALLOCATION and SINGLE_ALLOCATION wait for the assigned splitter
        if ist.workflow_step.step_kind in (StepKind.RUNTIME_SPLIT_ALLOCATION, StepKind.SINGLE_ALLOCATION):
            kind = ist.workflow_step.step_kind
            if ist.assigned_user_id:
                _emit_event(
                    instance,
                    WorkflowEventType.STEP_ASSIGNED,
                    actor_user,
                    target_user=ist.assigned_user,
                    metadata={"instance_step_id": ist.id, "workflow_step_id": ist.workflow_step_id, "kind": kind},
                )
            continue

        if ist.assigned_user_id:
            _emit_event(
                instance,
                WorkflowEventType.STEP_ASSIGNED,
                actor_user,
                target_user=ist.assigned_user,
                metadata={"instance_step_id": ist.id, "workflow_step_id": ist.workflow_step_id},
            )


def _sync_subject_status_on_workflow_change(instance):
    """
    Sync the subject domain object's status when the workflow state changes.

    Supported subject types:
        invoice  → Invoice.status
        campaign → Campaign.status

    Status mapping:
        ACTIVE   → in_review
        APPROVED → internally_approved + finance handoff created & sent
        REJECTED → rejected

    Note: APPROVED no longer sets a final "approved" state for invoice/campaign.
    Instead it transitions to internally_approved and triggers the generic
    finance handoff layer (apps.finance) for external finance review.

    Lazy imports avoid circular dependencies.
    """
    from apps.finance.models import FinanceHandoffStatus
    from apps.finance.services import (
        FinanceHandoffError,
        HandoffStateError,
        create_finance_handoff,
        send_finance_handoff,
    )

    if instance.subject_type == "invoice":
        from apps.invoices.models import Invoice, InvoiceStatus
        if instance.status == InstanceStatus.ACTIVE:
            Invoice.objects.filter(pk=instance.subject_id).update(
                status=InvoiceStatus.IN_REVIEW
            )
        elif instance.status == InstanceStatus.REJECTED:
            Invoice.objects.filter(pk=instance.subject_id).update(
                status=InvoiceStatus.REJECTED
            )
        elif instance.status == InstanceStatus.APPROVED:
            # Transition to internally_approved first
            Invoice.objects.filter(pk=instance.subject_id).update(
                status=InvoiceStatus.INTERNALLY_APPROVED
            )
            # Then create and send the finance handoff
            try:
                try:
                    invoice = Invoice.objects.get(pk=instance.subject_id)
                    export_data = {
                        "invoice_title": invoice.title,
                        "amount": str(invoice.amount),
                        "currency": invoice.currency,
                        "created_by": str(invoice.created_by_id) if invoice.created_by_id else None,
                    }
                except Invoice.DoesNotExist:
                    export_data = None
                handoff = create_finance_handoff(
                    module="invoice",
                    subject_type="invoice",
                    subject_id=instance.subject_id,
                    scope_node=instance.subject_scope_node,
                    org=instance.subject_scope_node.org,
                    submitted_by=instance.started_by,
                    export_data=export_data,
                )
                send_finance_handoff(handoff, triggered_by=instance.started_by)
                # Update to finance_pending after send
                Invoice.objects.filter(pk=instance.subject_id).update(
                    status=InvoiceStatus.FINANCE_PENDING
                )
            except FinanceHandoffError:
                # Keep the invoice internally approved and leave the handoff in
                # PENDING when finance recipients or email delivery are not
                # ready. Ops can fix assignments and resend from Finance Handoffs.
                pass

    elif instance.subject_type == "campaign":
        from apps.campaigns.models import Campaign, CampaignStatus
        if instance.status == InstanceStatus.ACTIVE:
            Campaign.objects.filter(pk=instance.subject_id).update(
                status=CampaignStatus.IN_REVIEW
            )
        elif instance.status == InstanceStatus.REJECTED:
            Campaign.objects.filter(pk=instance.subject_id).update(
                status=CampaignStatus.REJECTED
            )
        elif instance.status == InstanceStatus.APPROVED:
            # Transition to internally_approved first
            Campaign.objects.filter(pk=instance.subject_id).update(
                status=CampaignStatus.INTERNALLY_APPROVED
            )
            # Then create and send the finance handoff
            try:
                try:
                    campaign = Campaign.objects.get(pk=instance.subject_id)
                    export_data = {
                        "campaign_name": campaign.name,
                        "code": campaign.code,
                        "requested_amount": str(campaign.requested_amount),
                        "currency": campaign.currency,
                        "campaign_type": campaign.campaign_type,
                    }
                except Campaign.DoesNotExist:
                    export_data = None
                handoff = create_finance_handoff(
                    module="campaign",
                    subject_type="campaign",
                    subject_id=instance.subject_id,
                    scope_node=instance.subject_scope_node,
                    org=instance.subject_scope_node.org,
                    submitted_by=instance.started_by,
                    export_data=export_data,
                )
                send_finance_handoff(handoff, triggered_by=instance.started_by)
                # Update to finance_pending after send
                Campaign.objects.filter(pk=instance.subject_id).update(
                    status=CampaignStatus.FINANCE_PENDING
                )
            except FinanceHandoffError:
                # Keep the campaign internally approved and leave the handoff in
                # PENDING when finance recipients or email delivery are not
                # ready. Ops can fix assignments and resend from Finance Handoffs.
                pass


def _return_vendor_submission_for_correction(instance, acted_by, note="") -> bool:
    """
    If this workflow instance belongs to a vendor-submitted invoice and the
    rejection happened at the first group, return the submission to the vendor
    for correction instead of leaving only a dead rejected invoice.

    Returns True if a submission was updated, else False.
    """
    if instance.subject_type != "invoice":
        return False

    from apps.invoices.models import Invoice, VendorInvoiceSubmissionStatus

    try:
        invoice = Invoice.objects.select_related("submission").get(pk=instance.subject_id)
    except Invoice.DoesNotExist:
        return False

    try:
        submission = invoice.submission
    except Exception:
        return False

    first_group = (
        instance.instance_groups.order_by("step_group__display_order").first()
    )
    if not first_group or instance.current_group_id != first_group.id:
        return False

    message = (note or "").strip() or "Your invoice was returned for correction during internal review."
    submission.status = VendorInvoiceSubmissionStatus.NEEDS_CORRECTION
    submission.correction_note = message
    submission.correction_requested_by = acted_by
    submission.correction_requested_at = timezone.now()
    submission.validation_errors = [
        {"field": "_workflow", "message": message},
    ]
    submission.save(
        update_fields=[
            "status",
            "correction_note",
            "correction_requested_by",
            "correction_requested_at",
            "validation_errors",
            "updated_at",
        ]
    )
    return True


# ---------------------------------------------------------------------------
# Template / Version resolution  (Gap #1 fix)
# ---------------------------------------------------------------------------

def resolve_workflow_template_version(module, scope_node):
    """
    Walk-up resolver for workflow template version.

    Contract:
        1. Gate on module activation. Module must be active for subject node.
        2. Walk up from subject_scope_node toward org root (nearest wins).
        3. At each node, only active (is_active=True) templates are considered.
        4. Resolution order per node:
             a. If a default (is_default=True) template has a published version → return it.
             b. If no default, but exactly one active template has a published version → return it.
             c. If no default and multiple active templates have published versions → raise
                WorkflowNotConfiguredError (explicit selection is required).
             d. Otherwise → continue walking up.
        5. No matching template found anywhere → WorkflowNotConfiguredError.

    Raises:
        ModuleInactiveError        — module is not active for this node/ancestor chain.
        WorkflowNotConfiguredError — no published template found, or ambiguous non-default
                                     templates exist and explicit selection is required.
    """
    from apps.modules.services import resolve_module_activation

    if not resolve_module_activation(module, scope_node):
        raise ModuleInactiveError(
            f"Module '{module}' is inactive for node {scope_node.id}. "
            "Cannot resolve workflow template."
        )

    nodes_to_check = [scope_node] + list(get_ancestors(scope_node).order_by("-depth"))
    for node in nodes_to_check:
        active_templates = list(
            WorkflowTemplate.objects.filter(module=module, scope_node=node, is_active=True)
        )
        if not active_templates:
            continue

        # Prefer the designated default template if it has a published version.
        default_templates = [t for t in active_templates if t.is_default]
        if default_templates:
            default = default_templates[0]
            version = WorkflowTemplateVersion.objects.filter(
                template=default, status=VersionStatus.PUBLISHED
            ).first()
            if version:
                return version
            # Default exists but has no published version — skip this node, keep walking.
            continue

        # No default — collect all active templates that have a published version.
        templates_with_published = []
        for template in active_templates:
            version = WorkflowTemplateVersion.objects.filter(
                template=template, status=VersionStatus.PUBLISHED
            ).first()
            if version:
                templates_with_published.append((template, version))

        if len(templates_with_published) == 1:
            return templates_with_published[0][1]
        elif len(templates_with_published) > 1:
            raise WorkflowNotConfiguredError(
                f"Multiple active workflow templates with published versions exist for module "
                f"'{module}' at node {node.id} but none is marked as default. "
                "Explicit workflow selection is required."
            )
        # Zero published versions at this node — continue walking up.

    raise WorkflowNotConfiguredError(
        f"No published workflow template found for module '{module}' "
        f"at node {scope_node.id} or any ancestor."
    )


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------

def resolve_step_target_node(step, subject_scope_node):
    """
    Resolve which ScopeNode governs eligibility for a WorkflowStep.

    Policies:
        SUBJECT_NODE      → return subject_scope_node directly
        ANCESTOR_OF_TYPE  → walk up to first ancestor matching step.ancestor_node_type
        ORG_ROOT          → first node at depth=0 for the org
        FIXED_NODE        → step.fixed_scope_node
    """
    policy = step.scope_resolution_policy

    if policy == ScopeResolutionPolicy.SUBJECT_NODE:
        return subject_scope_node

    if policy == ScopeResolutionPolicy.FIXED_NODE:
        if not step.fixed_scope_node:
            raise ValueError(f"Step {step.id} uses FIXED_NODE policy but has no fixed_scope_node.")
        return step.fixed_scope_node

    if policy == ScopeResolutionPolicy.ORG_ROOT:
        root = ScopeNode.objects.filter(
            org=subject_scope_node.org, depth=0, is_active=True
        ).first()
        if not root:
            raise ValueError(f"No root node found for org {subject_scope_node.org_id}.")
        return root

    if policy == ScopeResolutionPolicy.ANCESTOR_OF_TYPE:
        if not step.ancestor_node_type:
            raise ValueError(
                f"Step {step.id} uses ANCESTOR_OF_TYPE but ancestor_node_type is empty."
            )
        if subject_scope_node.node_type == step.ancestor_node_type:
            return subject_scope_node
        for ancestor in get_ancestors(subject_scope_node).order_by("-depth"):
            if ancestor.node_type == step.ancestor_node_type:
                return ancestor
        raise ValueError(
            f"No ancestor of type '{step.ancestor_node_type}' found for node {subject_scope_node.id}."
        )

    raise ValueError(f"Unknown scope_resolution_policy: {policy}")


# ---------------------------------------------------------------------------
# Eligible user resolution
# ---------------------------------------------------------------------------

def get_eligible_users_for_step(step, subject_scope_node):
    """
    Return queryset of users eligible to act on a step.
    Eligibility = holds step.required_role at the resolved target node.
    """
    target_node = resolve_step_target_node(step, subject_scope_node)
    return get_users_with_role_at_node(step.required_role, target_node)


def validate_step_default_user(step, subject_scope_node):
    """
    Check whether step.default_user is eligible at resolution time.
    Returns (is_valid: bool, target_node: ScopeNode | None).
    """
    if not step.default_user:
        return False, None
    eligible = get_eligible_users_for_step(step, subject_scope_node)
    target_node = resolve_step_target_node(step, subject_scope_node)
    return eligible.filter(pk=step.default_user_id).exists(), target_node


# ---------------------------------------------------------------------------
# Step assignment override (used during draft creation from invoice)
# ---------------------------------------------------------------------------

def apply_step_assignment_overrides(instance, assignments, subject_scope_node):
    """
    Apply manual user assignments to instance steps before activation.

    assignments: dict of { str(workflow_step_id): user_id }
    Each user is validated against the eligible pool for the step.
    Invalid entries raise ValueError with details of all failures.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    errors = {}
    for step_id_str, user_id in assignments.items():
        try:
            step_id = int(step_id_str)
        except (TypeError, ValueError):
            errors[step_id_str] = "Invalid step ID"
            continue

        try:
            ist = WorkflowInstanceStep.objects.get(
                instance_group__instance=instance,
                workflow_step_id=step_id,
            )
        except WorkflowInstanceStep.DoesNotExist:
            errors[step_id_str] = f"Step {step_id} not found in this instance"
            continue

        try:
            new_user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            errors[step_id_str] = f"User {user_id} not found"
            continue

        eligible = get_eligible_users_for_step(ist.workflow_step, subject_scope_node)
        if not eligible.filter(pk=new_user.pk).exists():
            errors[step_id_str] = f"User {user_id} is not eligible for step {step_id}"
            continue

        ist.assigned_user = new_user
        ist.assignment_state = AssignmentState.ASSIGNED
        ist.save(update_fields=["assigned_user", "assignment_state"])

    if errors:
        raise ValueError(f"Assignment overrides failed: {errors}")

    return instance


# ---------------------------------------------------------------------------
# Instance creation  (Gap #3 fix — WAITING not ORPHANED during draft)
# ---------------------------------------------------------------------------

@transaction.atomic
def create_workflow_instance_draft(
    template_version, subject_type, subject_id, subject_scope_node, started_by=None
):
    """
    Create a DRAFT WorkflowInstance with all instance groups and steps cloned
    from the template version.

    Steps with a valid default_user get assigned_user set at creation.
    Steps without a valid default_user are left with assigned_user=None and
    status=WAITING — assignment must be completed before activation.

    ORPHANED is reserved for runtime invalidation, not draft creation.
    """
    instance = WorkflowInstance.objects.create(
        template_version=template_version,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_scope_node=subject_scope_node,
        status=InstanceStatus.DRAFT,
        started_by=started_by,
    )

    groups = template_version.step_groups.prefetch_related("steps").order_by("display_order")
    for group in groups:
        instance_group = WorkflowInstanceGroup.objects.create(
            instance=instance,
            step_group=group,
            display_order=group.display_order,
            status=GroupStatus.WAITING,
        )
        for step in group.steps.order_by("display_order"):
            assigned = None
            state = AssignmentState.ASSIGNMENT_REQUIRED

            if step.step_kind == StepKind.SPLIT_BY_SCOPE:
                # Split parent steps are system steps. Branch records get their
                # own assignees when the group becomes active.
                state = AssignmentState.ASSIGNED
            elif step.step_kind == StepKind.SINGLE_ALLOCATION:
                # Single-allocation steps follow the same user resolution as
                # RUNTIME_SPLIT_ALLOCATION — the splitter must be an eligible user.
                eligible = list(get_eligible_users_for_step(step, subject_scope_node))
                if step.default_user_id and any(u.pk == step.default_user_id for u in eligible):
                    assigned = step.default_user
                    state = AssignmentState.ASSIGNED
                if assigned is None:
                    if len(eligible) == 1:
                        assigned = eligible[0]
                        state = AssignmentState.ASSIGNED
                    elif len(eligible) == 0:
                        state = AssignmentState.NO_ELIGIBLE_USERS
            else:
                eligible = list(get_eligible_users_for_step(step, subject_scope_node))
                if step.default_user_id and any(u.pk == step.default_user_id for u in eligible):
                    assigned = step.default_user
                    state = AssignmentState.ASSIGNED
                if assigned is None:
                    if len(eligible) == 1:
                        assigned = eligible[0]
                        state = AssignmentState.ASSIGNED
                    elif len(eligible) == 0:
                        state = AssignmentState.NO_ELIGIBLE_USERS
            WorkflowInstanceStep.objects.create(
                instance_group=instance_group,
                workflow_step=step,
                assigned_user=assigned,
                assignment_state=state,
                status=StepStatus.WAITING,
            )

    return instance


# ---------------------------------------------------------------------------
# Instance activation  (Gap #2 + Gap #4 fix)
# ---------------------------------------------------------------------------

@transaction.atomic
def activate_workflow_instance(instance, activated_by):
    """
    Transition a DRAFT instance to ACTIVE.

    Validation (Gap #2):
        All instance steps must have an assigned_user. Any step with
        assigned_user=None blocks activation entirely.

    Events (Gap #4):
        On success, emits STEP_ASSIGNED WorkflowEvent + in_app NotificationDelivery
        for every assigned step in the first group.

    Invoice sync:
        If subject_type='invoice', invoice.status → IN_REVIEW.
    """
    if instance.status != InstanceStatus.DRAFT:
        raise ValueError(
            f"Cannot activate instance {instance.id}: current status is '{instance.status}'."
        )

    first_group = instance.instance_groups.order_by("display_order").first()
    if not first_group:
        raise ValueError(f"Instance {instance.id} has no step groups — cannot activate.")

    unassigned = WorkflowInstanceStep.objects.filter(
        instance_group__instance=instance,
        assigned_user__isnull=True,
    ).exclude(workflow_step__step_kind=StepKind.SPLIT_BY_SCOPE)
    if unassigned.exists():
        count = unassigned.count()
        no_eligible = unassigned.filter(assignment_state=AssignmentState.NO_ELIGIBLE_USERS).count()
        if no_eligible:
            raise ValueError(
                f"Cannot activate instance {instance.id}: "
                f"{no_eligible} step(s) have no eligible users (NO_ELIGIBLE_USERS) — "
                f"fix workflow configuration before activating. "
                f"{count} step(s) total have no assigned user."
            )
        raise ValueError(
            f"Cannot activate instance {instance.id}: "
            f"{count} step(s) have no assigned user. Assign all steps before activating."
        )

    first_group.status = GroupStatus.IN_PROGRESS
    first_group.save(update_fields=["status"])

    instance.status = InstanceStatus.ACTIVE
    instance.started_by = activated_by
    instance.started_at = timezone.now()
    instance.current_group = first_group
    instance.save(update_fields=["status", "started_by", "started_at", "current_group"])

    _activate_group_entry_steps(first_group, instance, activated_by)

    # Sync subject status
    _sync_subject_status_on_workflow_change(instance)

    return instance


# ---------------------------------------------------------------------------
# First-step resolution (used by pending-review / begin-review)
# ---------------------------------------------------------------------------

def get_first_actionable_step(template_version):
    """
    Return the first WorkflowStep in template_version that requires a human actor.

    SPLIT_BY_SCOPE and JOIN_BRANCHES are system steps with no direct human
    assignee — they are skipped. NORMAL_APPROVAL and RUNTIME_SPLIT_ALLOCATION
    both require a human and count as the first actionable step.

    Returns None if the template has no human steps.
    """
    for group in template_version.step_groups.prefetch_related("steps").order_by("display_order"):
        for step in group.steps.order_by("display_order"):
            if step.step_kind not in (StepKind.SPLIT_BY_SCOPE, StepKind.JOIN_BRANCHES):
                return step
    return None


# ---------------------------------------------------------------------------
# Allocation coverage (public accessor used by tests and API)
# ---------------------------------------------------------------------------

def check_invoice_allocation_coverage(invoice, instance) -> dict:
    """
    Return a coverage summary for an invoice workflow instance.

    Returns a dict with keys:
        covered          — bool: True iff sum of active allocations == invoice.amount
        coverage_amount  — Decimal
        invoice_amount   — Decimal
        gap              — Decimal (invoice_amount - coverage_amount; negative = over-allocated)
        allocation_count — int
        detail           — str
    """
    from apps.invoices.models import InvoiceAllocation, InvoiceAllocationStatus
    from django.db.models import Sum, Count

    active_statuses = (
        InvoiceAllocationStatus.SUBMITTED,
        InvoiceAllocationStatus.BRANCH_PENDING,
        InvoiceAllocationStatus.APPROVED,
    )
    agg = InvoiceAllocation.objects.filter(
        invoice=invoice,
        workflow_instance=instance,
        status__in=active_statuses,
    ).aggregate(total=Sum("amount"), count=Count("id"))

    coverage = agg["total"] or Decimal("0")
    count = agg["count"] or 0
    gap = invoice.amount - coverage

    return {
        "covered": coverage == invoice.amount,
        "coverage_amount": coverage,
        "invoice_amount": invoice.amount,
        "gap": gap,
        "allocation_count": count,
        "detail": f"{coverage} of {invoice.amount} covered across {count} allocation(s)",
    }


# ---------------------------------------------------------------------------
# Runtime: Approve step
# ---------------------------------------------------------------------------

@transaction.atomic
def approve_workflow_step(instance_step, acted_by, note=""):
    """
    Approve a workflow step.

    Rules:
    - Group must be IN_PROGRESS and step must be WAITING.
    - Actor must be the step's assigned_user.

    Group progression (from step_group.parallel_mode):
    - SINGLE           → this approval completes the group.
    - ALL_MUST_APPROVE → group completes only when ALL steps are APPROVED.
    - ANY_ONE_APPROVES → this approval completes the group; remaining WAITING
                         steps are SKIPPED.

    On group completion:
    - If another group follows → advance to it (IN_PROGRESS) and emit STEP_ASSIGNED.
    - If this was the last group → instance APPROVED, subject status synced.
    """
    _assert_step_actionable(instance_step)

    if instance_step.assigned_user_id != acted_by.pk:
        raise StepActionError(
            f"User {acted_by} is not the assigned user for step {instance_step.id}."
        )

    _assert_workflow_subject_vendor_not_on_hold(instance_step.instance_group.instance)

    now = timezone.now()
    instance_step.status = StepStatus.APPROVED
    instance_step.acted_at = now
    instance_step.note = note
    instance_step.save(update_fields=["status", "acted_at", "note"])

    instance = instance_step.instance_group.instance
    _emit_event(
        instance, WorkflowEventType.STEP_APPROVED, acted_by,
        target_user=acted_by,
        metadata={"instance_step_id": instance_step.id, "note": note},
    )

    group = instance_step.instance_group
    parallel_mode = group.step_group.parallel_mode
    group_complete = False

    if parallel_mode == ParallelMode.SINGLE:
        group_complete = True

    elif parallel_mode == ParallelMode.ALL_MUST_APPROVE:
        all_steps = list(group.instance_steps.all())
        group_complete = all(s.status == StepStatus.APPROVED for s in all_steps)

    elif parallel_mode == ParallelMode.ANY_ONE_APPROVES:
        # Skip all other WAITING steps in this group
        group.instance_steps.filter(
            status=StepStatus.WAITING
        ).exclude(pk=instance_step.pk).update(status=StepStatus.SKIPPED)
        group_complete = True

    if group_complete:
        _advance_on_group_complete(group, instance, acted_by)

    return instance_step


# ---------------------------------------------------------------------------
# Runtime: Reject step
# ---------------------------------------------------------------------------

@transaction.atomic
def reject_workflow_step(instance_step, acted_by, note=""):
    """
    Reject a workflow step.

    Rules:
    - Group must be IN_PROGRESS and step must be WAITING.
    - Actor must be the step's assigned_user.

    Rejection routing (from step_group.on_rejection_action):
    - TERMINATE  → instance REJECTED, subject status synced.
    - GO_TO_GROUP → reset all groups from target through current (inclusive);
                    target group becomes IN_PROGRESS; current stays WAITING;
                    emit STEP_ASSIGNED for target group's assigned steps.

    Reset rule:
        Groups BEFORE the target: untouched (stay APPROVED).
        Groups from target through current: status → WAITING, steps → WAITING.
        Target group: → IN_PROGRESS immediately after reset.
        Assignments in reset groups: preserved.
        Event history: preserved (new events appended).
    """
    _assert_step_actionable(instance_step)

    if instance_step.assigned_user_id != acted_by.pk:
        raise StepActionError(
            f"User {acted_by} is not the assigned user for step {instance_step.id}."
        )

    now = timezone.now()
    instance_step.status = StepStatus.REJECTED
    instance_step.acted_at = now
    instance_step.note = note
    instance_step.save(update_fields=["status", "acted_at", "note"])

    group = instance_step.instance_group
    instance = group.instance

    _emit_event(
        instance, WorkflowEventType.STEP_REJECTED, acted_by,
        target_user=acted_by,
        metadata={"instance_step_id": instance_step.id, "note": note},
    )

    rejection_action = group.step_group.on_rejection_action

    if rejection_action == RejectionAction.TERMINATE:
        group.status = GroupStatus.REJECTED
        group.save(update_fields=["status"])
        instance.status = InstanceStatus.REJECTED
        instance.completed_at = timezone.now()
        instance.save(update_fields=["status", "completed_at"])
        _release_allocation_budgets(instance, acted_by)
        _emit_event(instance, WorkflowEventType.INSTANCE_REJECTED, acted_by)
        _sync_subject_status_on_workflow_change(instance)
        _return_vendor_submission_for_correction(instance, acted_by, note)

    elif rejection_action == RejectionAction.GO_TO_GROUP:
        target_step_group = group.step_group.on_rejection_goto_group
        if not target_step_group:
            raise ValueError(
                f"Group {group.id} has GO_TO_GROUP rejection action "
                "but on_rejection_goto_group is not set."
            )

        try:
            target_instance_group = instance.instance_groups.get(step_group=target_step_group)
        except WorkflowInstanceGroup.DoesNotExist:
            raise ValueError(
                f"Target step group {target_step_group.id} has no corresponding "
                f"instance group in instance {instance.id}."
            )

        # Reset all groups from target through current (inclusive)
        # Use step_group__display_order (template-level) not instance-level display_order.
        # Swap gte/lte so the range is always valid regardless of direction:
        #   forward reset (target > current): lte=target, gte=current
        #   backward reset (target < current): lte=current, gte=target
        lo = min(target_step_group.display_order, group.step_group.display_order)
        hi = max(target_step_group.display_order, group.step_group.display_order)
        groups_to_reset = list(
            instance.instance_groups.filter(
                step_group__display_order__gte=lo,
                step_group__display_order__lte=hi,
            ).order_by("step_group__display_order")
        )

        for g in groups_to_reset:
            g.status = GroupStatus.WAITING
            g.save(update_fields=["status"])
            # Reset steps: clear acted_at, reset status; keep assignments
            g.instance_steps.all().update(
                status=StepStatus.WAITING, acted_at=None, note=""
            )

        # Advance target to IN_PROGRESS
        target_instance_group.status = GroupStatus.IN_PROGRESS
        target_instance_group.save(update_fields=["status"])

        instance.current_group = target_instance_group
        instance.save(update_fields=["current_group"])

        # Emit STEP_ASSIGNED for newly active target group steps
        for ist in target_instance_group.instance_steps.filter(assigned_user__isnull=False):
            _emit_event(
                instance, WorkflowEventType.STEP_ASSIGNED, acted_by,
                target_user=ist.assigned_user,
                metadata={
                    "instance_step_id": ist.id,
                    "reason": "rejection_reset",
                    "source_group_id": group.id,
                },
            )

    return instance_step


# ---------------------------------------------------------------------------
# Runtime: Reassign step
# ---------------------------------------------------------------------------

@transaction.atomic
def reassign_workflow_step(instance_step, new_user, reassigned_by, note=""):
    """
    Reassign a step to a new user.

    Permission:
        reassigned_by must have REASSIGN permission on the subject resource
        at instance.subject_scope_node or any ancestor.

    Eligibility:
        new_user must be in the eligible pool for the step resolved against
        instance.subject_scope_node.

    The step stays WAITING — it remains actionable after reassignment.
    Reassignment audit fields (reassigned_from_user, reassigned_at, reassigned_by)
    are updated in place. Event history is preserved.
    """
    from apps.access.models import PermissionAction, PermissionResource
    from apps.access.services import user_has_permission_including_ancestors

    instance = instance_step.instance_group.instance

    # Determine resource from subject_type
    resource_map = {
        "invoice": PermissionResource.INVOICE,
        "campaign": PermissionResource.CAMPAIGN,
        "vendor": PermissionResource.VENDOR,
        "budget": PermissionResource.BUDGET,
    }
    resource = resource_map.get(instance.subject_type)
    if resource and not user_has_permission_including_ancestors(
        reassigned_by, PermissionAction.REASSIGN, resource, instance.subject_scope_node
    ):
        raise StepActionError(
            f"User {reassigned_by} does not have reassign:{instance.subject_type} "
            f"permission at node {instance.subject_scope_node} or any ancestor."
        )

    # Eligibility check
    eligible = get_eligible_users_for_step(
        instance_step.workflow_step, instance.subject_scope_node
    )
    if not eligible.filter(pk=new_user.pk).exists():
        raise StepActionError(
            f"User {new_user} is not eligible for step {instance_step.id}: "
            f"they do not hold the required role at the resolved target node."
        )

    old_user = instance_step.assigned_user
    now = timezone.now()

    instance_step.reassigned_from_user = old_user
    instance_step.reassigned_at = now
    instance_step.reassigned_by = reassigned_by
    instance_step.assigned_user = new_user
    instance_step.assignment_state = AssignmentState.ASSIGNED
    instance_step.save(update_fields=[
        "assigned_user", "assignment_state", "reassigned_from_user", "reassigned_at", "reassigned_by",
    ])

    _emit_event(
        instance, WorkflowEventType.STEP_REASSIGNED, reassigned_by,
        target_user=new_user,
        metadata={
            "instance_step_id": instance_step.id,
            "old_user_id": old_user.pk if old_user else None,
            "note": note,
        },
    )

    return instance_step


# ---------------------------------------------------------------------------
# Version lifecycle
# ---------------------------------------------------------------------------

@transaction.atomic
def publish_template_version(version, published_by):
    """
    Publish a DRAFT version. Archives the currently published version (if any).
    Relies on the DB partial unique constraint as the final safety net.
    """
    if version.status != VersionStatus.DRAFT:
        raise ValueError(f"Only DRAFT versions can be published. Got: {version.status}")

    WorkflowTemplateVersion.objects.filter(
        template=version.template,
        status=VersionStatus.PUBLISHED,
    ).update(status=VersionStatus.ARCHIVED)

    version.status = VersionStatus.PUBLISHED
    version.published_at = timezone.now()
    version.published_by = published_by
    version.save(update_fields=["status", "published_at", "published_by"])
    return version


@transaction.atomic
def archive_template_version(version):
    """Move a PUBLISHED or DRAFT version to ARCHIVED."""
    if version.status not in (VersionStatus.PUBLISHED, VersionStatus.DRAFT):
        raise ValueError(f"Cannot archive version with status '{version.status}'.")
    version.status = VersionStatus.ARCHIVED
    version.save(update_fields=["status"])
    return version


# ---------------------------------------------------------------------------
# Split / Join runtime
# ---------------------------------------------------------------------------

def _resolve_split_branch_nodes(step: WorkflowStep, subject_scope_node: ScopeNode) -> list[ScopeNode]:
    """
    Resolve the list of scope nodes that will each receive one branch.

    split_target_mode modes:
        EXPLICIT_NODES  — use the pre-configured split_target_nodes list
        CHILD_NODES     — use direct children of subject_scope_node

    Returns list of ScopeNode objects (frozen at split time).
    Duplicate node IDs are deduplicated.
    """
    if step.split_target_mode == "EXPLICIT_NODES":
        node_ids = step.split_target_nodes or []
        nodes = list(ScopeNode.objects.filter(id__in=node_ids, is_active=True))
        # Deduplicate by id
        seen = set()
        unique = []
        for n in nodes:
            if n.id not in seen:
                seen.add(n.id)
                unique.append(n)
        return unique

    elif step.split_target_mode == "CHILD_NODES":
        return list(
            ScopeNode.objects.filter(parent=subject_scope_node, is_active=True).order_by("name")
        )

    # Fallback: direct children
    return list(
        ScopeNode.objects.filter(parent=subject_scope_node, is_active=True).order_by("name")
    )


def _assign_branch_user(branch, step: WorkflowStep, subject_scope_node: ScopeNode):
    """Resolve and assign the user for one branch."""
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Branch approvals are scoped to the branch target node, not the original
    # invoice/campaign subject node.
    eligible = list(get_users_with_role_at_node(step.required_role, branch.target_scope_node))

    assigned = None
    state = AssignmentState.ASSIGNMENT_REQUIRED

    if step.default_user_id and any(u.pk == step.default_user_id for u in eligible):
        assigned = step.default_user
        state = AssignmentState.ASSIGNED
    elif len(eligible) == 1:
        assigned = eligible[0]
        state = AssignmentState.ASSIGNED
    elif len(eligible) == 0:
        state = AssignmentState.NO_ELIGIBLE_USERS

    branch.assigned_user = assigned
    branch.assignment_state = state
    branch.save(update_fields=["assigned_user", "assignment_state"])
    return branch


@transaction.atomic
def split_instance_step(instance_step: WorkflowInstanceStep) -> list[WorkflowInstanceBranch]:
    """
    Activate a SPLIT_BY_SCOPE step: freeze branch targets and create branch records.

    1. Resolve target scope nodes (frozen at split time).
    2. Create one WorkflowInstanceBranch per target node.
    3. Assign eligible user to each branch.
    4. Mark the parent step WAITING_BRANCHES (not yet complete).
    5. Emit BRANCHES_SPLIT event.
    6. Emit BRANCH_ASSIGNED for each branch with a resolved user.

    Returns list of created branches.

    Idempotent: if branches already exist for this step, returns existing branches
    without creating duplicates.
    """
    step = instance_step.workflow_step
    instance = instance_step.instance_group.instance
    subject_scope_node = instance.subject_scope_node

    if step.step_kind != "SPLIT_BY_SCOPE":
        raise ValueError(
            f"split_instance_step called on step {step.id} with kind '{step.step_kind}', "
            "expected 'SPLIT_BY_SCOPE'."
        )

    # Idempotency: don't re-split
    existing = list(instance_step.branches.all())
    if existing:
        return existing

    branch_nodes = _resolve_split_branch_nodes(step, subject_scope_node)

    branches = []
    for idx, node in enumerate(branch_nodes):
        branch = WorkflowInstanceBranch.objects.create(
            parent_instance_step=instance_step,
            instance=instance,
            target_scope_node=node,
            branch_index=idx,
            status=BranchStatus.PENDING,
        )
        # Resolve assignee
        _assign_branch_user(branch, step, subject_scope_node)
        branches.append(branch)

    # Mark step as waiting on branches
    instance_step.status = StepStatus.WAITING_BRANCHES
    instance_step.save(update_fields=["status"])

    # Emit BRANCHES_SPLIT
    _emit_event(
        instance,
        WorkflowEventType.BRANCHES_SPLIT,
        actor_user=instance.started_by,
        metadata={
            "instance_step_id": instance_step.id,
            "workflow_step_id": step.id,
            "branch_count": len(branches),
            "branch_node_ids": [b.target_scope_node_id for b in branches],
        },
    )

    # Emit BRANCH_ASSIGNED for each branch that has a resolved user
    for branch in branches:
        if branch.assigned_user:
            _emit_event(
                instance,
                WorkflowEventType.BRANCH_ASSIGNED,
                actor_user=instance.started_by,
                target_user=branch.assigned_user,
                metadata={"branch_id": branch.id},
            )

    return branches


@transaction.atomic
def approve_workflow_branch(branch: WorkflowInstanceBranch, acted_by, note=""):
    """
    Approve one branch of a split step.

    Rules:
    - Branch must be PENDING.
    - Actor must be the branch's assigned_user.

    On approval:
    - Mark branch APPROVED, record acted_at and note.
    - Emit BRANCH_APPROVED event.

    Join check (ALL_BRANCHES_MUST_COMPLETE):
    - After approving, check if ALL branches from the same parent step are done.
    - If any branch is still PENDING → do nothing more; parent stays WAITING_BRANCHES.
    - If ALL branches are APPROVED (none rejected) → advance parent step to APPROVED,
      advance group (via _advance_on_group_complete), emit BRANCHES_JOINED.
    - If ANY branch is REJECTED → apply rejection policy: return control to the
      split owner (the user who started the instance) with branch rejection summary.
    """
    if branch.status != BranchStatus.PENDING:
        raise StepActionError(
            f"Branch {branch.id} is not PENDING — cannot approve. Current status: '{branch.status}'."
        )

    instance = branch.instance
    if branch.assigned_user_id != acted_by.pk:
        raise StepActionError(
            f"User {acted_by} is not the assigned user for branch {branch.id}."
        )

    _assert_workflow_subject_vendor_not_on_hold(instance)

    now = timezone.now()
    branch.status = BranchStatus.APPROVED
    branch.acted_at = now
    branch.note = note
    branch.save(update_fields=["status", "acted_at", "note"])

    # Update linked allocation status for RUNTIME_SPLIT_ALLOCATION branches.
    allocation = getattr(branch, "invoice_allocation", None)
    if allocation:
        allocation.status = "approved"
        allocation.approved_by = acted_by
        allocation.approved_at = now
        allocation.save(update_fields=["status", "approved_by", "approved_at"])

    _emit_event(
        instance,
        WorkflowEventType.BRANCH_APPROVED,
        actor_user=acted_by,
        metadata={"branch_id": branch.id, "note": note},
    )

    # Join check: all branches must be done before advancing
    parent_step = branch.parent_instance_step
    all_branches = list(parent_step.branches.all())

    if all(b.status == BranchStatus.APPROVED for b in all_branches):
        # All approved — advance parent step
        parent_step.status = StepStatus.APPROVED
        parent_step.acted_at = now
        parent_step.note = f"All {len(all_branches)} branches approved."
        parent_step.save(update_fields=["status", "acted_at", "note"])

        _emit_event(
            instance,
            WorkflowEventType.BRANCHES_JOINED,
            actor_user=acted_by,
            metadata={
                "instance_step_id": parent_step.id,
                "branch_count": len(all_branches),
            },
        )

        # Advance the group
        group = parent_step.instance_group
        _advance_on_group_complete(group, instance, acted_by)

    elif any(b.status == BranchStatus.REJECTED for b in all_branches):
        # Any rejection → return control to split owner
        # Mark split step as REJECTED so parent flow can route appropriately
        parent_step.status = StepStatus.REJECTED
        parent_step.acted_at = now
        rejected_branches = [b for b in all_branches if b.status == BranchStatus.REJECTED]
        parent_step.note = f"{len(rejected_branches)} of {len(all_branches)} branches rejected."
        parent_step.save(update_fields=["status", "acted_at", "note"])

        _emit_event(
            instance,
            WorkflowEventType.BRANCH_REJECTED,
            actor_user=acted_by,
            metadata={
                "instance_step_id": parent_step.id,
                "rejected_branch_ids": [b.id for b in rejected_branches],
            },
        )

        # Rejection on split: terminate the instance
        group = parent_step.instance_group
        group.status = GroupStatus.REJECTED
        group.save(update_fields=["status"])
        instance.status = InstanceStatus.REJECTED
        instance.completed_at = now
        instance.save(update_fields=["status", "completed_at"])
        _emit_event(instance, WorkflowEventType.INSTANCE_REJECTED, acted_by)
        _sync_subject_status_on_workflow_change(instance)

    # If some still PENDING, do nothing — parent stays WAITING_BRANCHES

    return branch


@transaction.atomic
def reject_workflow_branch(branch: WorkflowInstanceBranch, acted_by, note=""):
    """
    Reject one branch of a split step.

    Rules:
    - Branch must be PENDING.
    - Actor must be the branch's assigned_user.

    For RUNTIME_SPLIT_ALLOCATION branches: delegates to handle_runtime_split_branch_rejection
    which honors the configured rejection_action (BRANCH_CORRECTION returns to splitter;
    TERMINATE kills the instance).

    For legacy SPLIT_BY_SCOPE branches: terminates the instance (original behavior).
    """
    if branch.status != BranchStatus.PENDING:
        raise StepActionError(
            f"Branch {branch.id} is not PENDING — cannot reject. Current status: '{branch.status}'."
        )

    instance = branch.instance
    if branch.assigned_user_id != acted_by.pk:
        raise StepActionError(
            f"User {acted_by} is not the assigned user for branch {branch.id}."
        )

    now = timezone.now()
    branch.status = BranchStatus.REJECTED
    branch.acted_at = now
    branch.note = note
    branch.rejection_reason = note
    branch.save(update_fields=["status", "acted_at", "note", "rejection_reason"])

    parent_step = branch.parent_instance_step

    # Runtime split allocation branches use configurable rejection behavior
    if parent_step.workflow_step.step_kind == StepKind.RUNTIME_SPLIT_ALLOCATION:
        from apps.workflow.services_split import handle_runtime_split_branch_rejection
        handle_runtime_split_branch_rejection(branch, acted_by, note)
        return branch

    # Legacy SPLIT_BY_SCOPE: terminate on rejection (original behavior)
    _emit_event(
        instance,
        WorkflowEventType.BRANCH_REJECTED,
        actor_user=acted_by,
        metadata={"branch_id": branch.id, "note": note},
    )

    parent_step.status = StepStatus.REJECTED
    parent_step.acted_at = now
    parent_step.note = note
    parent_step.save(update_fields=["status", "acted_at", "note"])

    group = parent_step.instance_group
    group.status = GroupStatus.REJECTED
    group.save(update_fields=["status"])

    instance.status = InstanceStatus.REJECTED
    instance.completed_at = now
    instance.save(update_fields=["status", "completed_at"])

    _emit_event(instance, WorkflowEventType.INSTANCE_REJECTED, acted_by)
    _sync_subject_status_on_workflow_change(instance)

    return branch


@transaction.atomic
def reassign_workflow_branch(branch: WorkflowInstanceBranch, new_user, reassigned_by, note=""):
    """
    Reassign a branch to a new user.

    Permission: reassigned_by needs reassign permission on the subject resource.
    Eligibility: new_user must be eligible for the parent step's required_role
                 at the branch's target_scope_node.
    """
    from apps.access.models import PermissionAction, PermissionResource
    from apps.access.services import user_has_permission_including_ancestors

    instance = branch.instance
    step = branch.parent_instance_step.workflow_step

    resource_map = {
        "invoice": PermissionResource.INVOICE,
        "campaign": PermissionResource.CAMPAIGN,
        "vendor": PermissionResource.VENDOR,
        "budget": PermissionResource.BUDGET,
    }
    resource = resource_map.get(instance.subject_type)
    if resource and not user_has_permission_including_ancestors(
        reassigned_by, PermissionAction.REASSIGN, resource, instance.subject_scope_node
    ):
        raise StepActionError(
            f"User {reassigned_by} does not have reassign:{instance.subject_type} "
            f"permission at node {instance.subject_scope_node}."
        )

    # Eligibility: check at target scope node for the required role
    eligible = get_users_with_role_at_node(step.required_role, branch.target_scope_node)
    if not eligible.filter(pk=new_user.pk).exists():
        raise StepActionError(
            f"User {new_user} is not eligible for branch {branch.id}: "
            f"they do not hold required role '{step.required_role.name}' "
            f"at target node {branch.target_scope_node}."
        )

    old_user = branch.assigned_user
    now = timezone.now()

    branch.reassigned_from_user = old_user
    branch.reassigned_at = now
    branch.reassigned_by = reassigned_by
    branch.assigned_user = new_user
    branch.assignment_state = AssignmentState.ASSIGNED
    branch.save(update_fields=[
        "assigned_user", "assignment_state", "reassigned_from_user", "reassigned_at", "reassigned_by",
    ])

    _emit_event(
        instance,
        WorkflowEventType.BRANCH_REASSIGNED,
        actor_user=reassigned_by,
        target_user=new_user,
        metadata={
            "branch_id": branch.id,
            "old_user_id": old_user.pk if old_user else None,
            "note": note,
        },
    )

    return branch
