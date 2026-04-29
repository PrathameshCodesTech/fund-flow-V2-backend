from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from decimal import Decimal, InvalidOperation
from datetime import date

from apps.budgets.models import (
    Budget,
    BudgetLine,
    BudgetRule,
    BudgetCategory,
    BudgetSubCategory,
    BudgetConsumption,
    BudgetVarianceRequest,
    BudgetImportBatch,
    BudgetImportRow,
    ConsumptionType,
    ConsumptionStatus,
    VarianceStatus,
    BudgetStatus,
    PeriodType,
    ImportBatchStatus,
    ImportRowStatus,
    ImportMode,
    SourceType,
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class BudgetLimitExceeded(ValueError):
    """Raised when a reservation would exceed the hard block threshold."""


class BudgetNotActiveError(ValueError):
    """Raised when a budget is not in ACTIVE status."""


class BudgetLineNotFoundError(ValueError):
    """Raised when no matching BudgetLine exists for an allocation."""


# ---------------------------------------------------------------------------
# BudgetLine resolver
# ---------------------------------------------------------------------------

def resolve_budget_line_for_allocation(
    budget: "Budget",
    category_id: int | None = None,
    subcategory_id: int | None = None,
) -> "BudgetLine":
    """
    Resolve the correct BudgetLine for an allocation with category/subcategory context.

    Resolution rules:
        - Exact match (category_id + subcategory_id if subcategory is not null)
        - If subcategory_id is null and category_id is not null: match on category_id alone
        - If both null: return the first available line (fallback for legacy callers
          with single-line budgets; callers SHOULD specify category/subcategory)
        - No match: raises BudgetLineNotFoundError

    Raises:
        BudgetLineNotFoundError — no matching line exists for the given combination
    """
    from apps.budgets.models import BudgetLine

    filters: dict = {"budget": budget}

    if subcategory_id is not None:
        filters["category_id"] = category_id
        filters["subcategory_id"] = subcategory_id
    elif category_id is not None:
        filters["category_id"] = category_id
        filters["subcategory_id__isnull"] = True
    else:
        # Fallback: return first line on this budget (legacy path for campaigns
        # that don't carry category context; use of this fallback is discouraged)
        lines = BudgetLine.objects.filter(budget=budget).order_by("id")
        if lines.exists():
            return lines.first()
        raise BudgetLineNotFoundError(
            f"Budget {budget.id} has no budget lines. "
            "At least one budget line must exist before allocations can be made."
        )

    lines = BudgetLine.objects.filter(**filters)
    if lines.exists():
        return lines.first()

    if subcategory_id is not None:
        raise BudgetLineNotFoundError(
            f"No BudgetLine found for budget={budget.id}, "
            f"category={category_id}, subcategory={subcategory_id}. "
            f"Reserve against an existing budget line first."
        )
    else:
        raise BudgetLineNotFoundError(
            f"No BudgetLine found for budget={budget.id}, category={category_id} "
            f"(category-level, no subcategory). "
            f"Reserve against an existing budget line first."
        )


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------

def calculate_projected_utilization_for_line(line: BudgetLine, amount: Decimal):
    """
    Returns (current_utilization_percent, projected_utilization_percent) for a line.
    """
    if line.allocated_amount <= 0:
        current = Decimal("0")
        projected = Decimal("0")
    else:
        current = ((line.reserved_amount + line.consumed_amount) / line.allocated_amount) * 100
        projected = ((line.reserved_amount + line.consumed_amount + amount) / line.allocated_amount) * 100
    return current, projected


def get_source_reserved_balance_for_line(line: BudgetLine, source_type: str, source_id: str) -> Decimal:
    """
    Net reserved balance for a specific (budget_line, source_type, source_id) tuple.
    Net = sum(RESERVED) - sum(CONSUMED) - sum(RELEASED) across all applied rows.
    """
    rows = BudgetConsumption.objects.filter(
        budget_line=line,
        source_type=source_type,
        source_id=str(source_id),
        status=ConsumptionStatus.APPLIED,
    )
    reserved = rows.filter(consumption_type=ConsumptionType.RESERVED).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    consumed = rows.filter(consumption_type=ConsumptionType.CONSUMED).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    released = rows.filter(consumption_type=ConsumptionType.RELEASED).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    return reserved - consumed - released


def get_source_reserved_balance(budget: Budget, source_type: str, source_id: str) -> Decimal:
    """
    Net reserved balance for a specific (budget, source_type, source_id) tuple.
    Used for header-level operations (backward compat).
    """
    from django.db.models import Sum
    rows = BudgetConsumption.objects.filter(
        budget=budget,
        source_type=source_type,
        source_id=str(source_id),
        status=ConsumptionStatus.APPLIED,
    )
    reserved = rows.filter(consumption_type=ConsumptionType.RESERVED).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    consumed = rows.filter(consumption_type=ConsumptionType.CONSUMED).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    released = rows.filter(consumption_type=ConsumptionType.RELEASED).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    return reserved - consumed - released


def _get_rule(budget: Budget) -> BudgetRule | None:
    """Return the active rule for a budget, or None for defaults."""
    try:
        return budget.rule if budget.rule.is_active else None
    except BudgetRule.DoesNotExist:
        return None


# ---------------------------------------------------------------------------
# Reserve budget line
# ---------------------------------------------------------------------------

@transaction.atomic
def reserve_budget_line(
    line: BudgetLine,
    amount: Decimal,
    source_type: str,
    source_id: str,
    requested_by,
    note: str = "",
) -> dict:
    """
    Attempt to reserve `amount` from a BudgetLine.

    Returns a dict with keys:
        status: "reserved" | "reserved_with_warning" | "variance_required"
        consumption: BudgetConsumption | None
        variance_request: BudgetVarianceRequest | None
        projected_utilization: Decimal
        current_utilization: Decimal

    Raises:
        BudgetNotActiveError — if line.budget.status != ACTIVE
        BudgetLimitExceeded — if projected > hard_block_threshold
        ValueError — if amount <= 0
    """
    # Refresh both objects for concurrency safety
    line.refresh_from_db()
    line.budget.refresh_from_db()

    budget = line.budget

    if budget.status != BudgetStatus.ACTIVE:
        raise BudgetNotActiveError(
            f"Budget {budget.id} is {budget.status}, expected ACTIVE."
        )

    if amount <= 0:
        raise ValueError("Reservation amount must be greater than zero.")

    rule = _get_rule(budget)
    if rule:
        warning = rule.warning_threshold_percent
        approval = rule.approval_threshold_percent
        hard_block = rule.hard_block_threshold_percent
    else:
        warning = Decimal("80.00")
        approval = Decimal("100.00")
        hard_block = Decimal("110.00")

    current_util, projected_util = calculate_projected_utilization_for_line(line, amount)

    if projected_util >= hard_block:
        raise BudgetLimitExceeded(
            f"Reservation of {amount} would bring projected utilization to "
            f"{projected_util:.2f}%, exceeding hard block threshold of "
            f"{hard_block:.2f}%. Variance approval required."
        )

    if projected_util >= approval:
        variance_req = BudgetVarianceRequest.objects.create(
            budget=budget,
            budget_line=line,
            source_type=source_type,
            source_id=str(source_id),
            requested_amount=amount,
            current_utilization_percent=current_util,
            projected_utilization_percent=projected_util,
            reason=note,
            status=VarianceStatus.PENDING,
            requested_by=requested_by,
        )
        return {
            "status": "variance_required",
            "consumption": None,
            "variance_request": variance_req,
            "projected_utilization": projected_util,
            "current_utilization": current_util,
        }

    consumption = BudgetConsumption.objects.create(
        budget=budget,
        budget_line=line,
        source_type=source_type,
        source_id=str(source_id),
        amount=amount,
        consumption_type=ConsumptionType.RESERVED,
        status=ConsumptionStatus.APPLIED,
        created_by=requested_by,
        note=note,
    )
    line.reserved_amount += amount
    line.save(update_fields=["reserved_amount", "updated_at"])
    budget.reserved_amount += amount
    budget.save(update_fields=["reserved_amount", "updated_at"])

    if projected_util >= warning:
        return {
            "status": "reserved_with_warning",
            "consumption": consumption,
            "variance_request": None,
            "projected_utilization": projected_util,
            "current_utilization": current_util,
        }

    return {
        "status": "reserved",
        "consumption": consumption,
        "variance_request": None,
        "projected_utilization": projected_util,
        "current_utilization": current_util,
    }


# ---------------------------------------------------------------------------
# Consume reserved budget line
# ---------------------------------------------------------------------------

@transaction.atomic
def consume_reserved_budget_line(
    line: BudgetLine,
    amount: Decimal,
    source_type: str,
    source_id: str,
    consumed_by,
    note: str = "",
) -> dict:
    """
    Convert a portion (or all) of a reserved amount into consumed on a BudgetLine.
    """
    line.refresh_from_db()
    line.budget.refresh_from_db()
    budget = line.budget

    if budget.status not in (BudgetStatus.ACTIVE, BudgetStatus.EXHAUSTED):
        raise BudgetNotActiveError(
            f"Budget {budget.id} is {budget.status}, cannot consume."
        )

    if amount <= 0:
        raise ValueError("Consumption amount must be greater than zero.")

    source_balance = get_source_reserved_balance_for_line(line, source_type, str(source_id))
    if amount > source_balance:
        raise ValueError(
            f"Cannot consume {amount}: only {source_balance} reserved for "
            f"source {source_type}/{source_id} on this budget line."
        )

    consumption = BudgetConsumption.objects.create(
        budget=budget,
        budget_line=line,
        source_type=source_type,
        source_id=str(source_id),
        amount=amount,
        consumption_type=ConsumptionType.CONSUMED,
        status=ConsumptionStatus.APPLIED,
        created_by=consumed_by,
        note=note,
    )

    line.reserved_amount -= amount
    line.consumed_amount += amount
    line.save(update_fields=["reserved_amount", "consumed_amount", "updated_at"])
    budget.reserved_amount -= amount
    budget.consumed_amount += amount
    budget.save(update_fields=["reserved_amount", "consumed_amount", "updated_at"])

    return {
        "status": "consumed",
        "consumption": consumption,
    }


# ---------------------------------------------------------------------------
# Release reserved budget line
# ---------------------------------------------------------------------------

@transaction.atomic
def release_reserved_budget_line(
    line: BudgetLine,
    amount: Decimal,
    source_type: str,
    source_id: str,
    released_by,
    note: str = "",
) -> dict:
    """
    Release a previously reserved amount back to available pool on a BudgetLine.
    """
    line.refresh_from_db()
    line.budget.refresh_from_db()
    budget = line.budget

    if budget.status not in (BudgetStatus.ACTIVE, BudgetStatus.EXHAUSTED):
        raise BudgetNotActiveError(
            f"Budget {budget.id} is {budget.status}, cannot release."
        )

    if amount <= 0:
        raise ValueError("Release amount must be greater than zero.")

    source_balance = get_source_reserved_balance_for_line(line, source_type, str(source_id))
    if amount > source_balance:
        raise ValueError(
            f"Cannot release {amount}: only {source_balance} reserved for "
            f"source {source_type}/{source_id} on this budget line."
        )

    consumption = BudgetConsumption.objects.create(
        budget=budget,
        budget_line=line,
        source_type=source_type,
        source_id=str(source_id),
        amount=amount,
        consumption_type=ConsumptionType.RELEASED,
        status=ConsumptionStatus.APPLIED,
        created_by=released_by,
        note=note,
    )

    line.reserved_amount -= amount
    line.save(update_fields=["reserved_amount", "updated_at"])
    budget.reserved_amount -= amount
    budget.save(update_fields=["reserved_amount", "updated_at"])

    return {
        "status": "released",
        "consumption": consumption,
    }


# ---------------------------------------------------------------------------
# Review variance request
# ---------------------------------------------------------------------------

@transaction.atomic
def review_variance_request(
    variance_request: BudgetVarianceRequest,
    decision: str,
    reviewed_by,
    review_note: str = "",
) -> BudgetVarianceRequest:
    """
    Approve or reject a pending variance request.

    If approved and the request is line-level (budget_line is set):
        - creates BudgetConsumption linked to both budget and budget_line
        - increments both line.reserved_amount and budget.reserved_amount

    If approved and budget-level only (legacy, budget_line is None):
        - creates BudgetConsumption linked to budget only
        - increments budget.reserved_amount

    If rejected: no budget changes.
    """
    if variance_request.status != VarianceStatus.PENDING:
        raise ValueError(
            f"Variance request {variance_request.id} is {variance_request.status}, "
            "expected PENDING."
        )

    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be 'approved' or 'rejected', got: {decision}")

    variance_request.reviewed_by = reviewed_by
    variance_request.reviewed_at = timezone.now()
    variance_request.review_note = review_note

    if decision == "approved":
        variance_request.status = VarianceStatus.APPROVED
        variance_request.save()

        line = variance_request.budget_line
        budget = variance_request.budget

        BudgetConsumption.objects.create(
            budget=budget,
            budget_line=line,
            source_type=variance_request.source_type,
            source_id=str(variance_request.source_id),
            amount=variance_request.requested_amount,
            consumption_type=ConsumptionType.RESERVED,
            status=ConsumptionStatus.APPLIED,
            created_by=reviewed_by,
            note=f"Approved variance request {variance_request.id}: {review_note}",
        )

        if line is not None:
            line.reserved_amount += variance_request.requested_amount
            line.save(update_fields=["reserved_amount", "updated_at"])

        budget.reserved_amount += variance_request.requested_amount
        budget.save(update_fields=["reserved_amount", "updated_at"])

    else:
        variance_request.status = VarianceStatus.REJECTED
        variance_request.save()

    return variance_request


# ---------------------------------------------------------------------------
# Header-level backward compat functions
# (used by campaign and workflow services that operate on Budget headers)
# ---------------------------------------------------------------------------

def calculate_projected_utilization(budget: Budget, amount: Decimal):
    """Header-level utilization projection (backward compat)."""
    if budget.allocated_amount <= 0:
        current = Decimal("0")
        projected = Decimal("0")
    else:
        current = ((budget.reserved_amount + budget.consumed_amount) / budget.allocated_amount) * 100
        projected = ((budget.reserved_amount + budget.consumed_amount + amount) / budget.allocated_amount) * 100
    return current, projected


@transaction.atomic
def reserve_budget(
    budget: Budget,
    amount: Decimal,
    source_type: str,
    source_id: str,
    requested_by,
    note: str = "",
) -> dict:
    """
    Header-level budget reservation (backward compat for campaign/workflow callers).
    Operates directly on the Budget header without targeting a specific BudgetLine.
    """
    budget.refresh_from_db()

    if budget.status != BudgetStatus.ACTIVE:
        raise BudgetNotActiveError(
            f"Budget {budget.id} is {budget.status}, expected ACTIVE."
        )
    if amount <= 0:
        raise ValueError("Reservation amount must be greater than zero.")

    rule = _get_rule(budget)
    if rule:
        warning = rule.warning_threshold_percent
        approval = rule.approval_threshold_percent
        hard_block = rule.hard_block_threshold_percent
    else:
        warning = Decimal("80.00")
        approval = Decimal("100.00")
        hard_block = Decimal("110.00")

    current_util, projected_util = calculate_projected_utilization(budget, amount)

    if projected_util >= hard_block:
        raise BudgetLimitExceeded(
            f"Reservation of {amount} would bring projected utilization to "
            f"{projected_util:.2f}%, exceeding hard block threshold of "
            f"{hard_block:.2f}%. Variance approval required."
        )

    if projected_util >= approval:
        variance_req = BudgetVarianceRequest.objects.create(
            budget=budget,
            budget_line=None,
            source_type=source_type,
            source_id=str(source_id),
            requested_amount=amount,
            current_utilization_percent=current_util,
            projected_utilization_percent=projected_util,
            reason=note,
            status=VarianceStatus.PENDING,
            requested_by=requested_by,
        )
        return {
            "status": "variance_required",
            "consumption": None,
            "variance_request": variance_req,
            "projected_utilization": projected_util,
            "current_utilization": current_util,
        }

    consumption = BudgetConsumption.objects.create(
        budget=budget,
        budget_line=None,
        source_type=source_type,
        source_id=str(source_id),
        amount=amount,
        consumption_type=ConsumptionType.RESERVED,
        status=ConsumptionStatus.APPLIED,
        created_by=requested_by,
        note=note,
    )
    budget.reserved_amount += amount
    budget.save(update_fields=["reserved_amount", "updated_at"])

    if projected_util >= warning:
        return {
            "status": "reserved_with_warning",
            "consumption": consumption,
            "variance_request": None,
            "projected_utilization": projected_util,
            "current_utilization": current_util,
        }

    return {
        "status": "reserved",
        "consumption": consumption,
        "variance_request": None,
        "projected_utilization": projected_util,
        "current_utilization": current_util,
    }


@transaction.atomic
def consume_reserved_budget(
    budget: Budget,
    amount: Decimal,
    source_type: str,
    source_id: str,
    consumed_by,
    note: str = "",
) -> dict:
    """Header-level consume (backward compat)."""
    if budget.status not in (BudgetStatus.ACTIVE, BudgetStatus.EXHAUSTED):
        raise BudgetNotActiveError(
            f"Budget {budget.id} is {budget.status}, cannot consume."
        )
    if amount <= 0:
        raise ValueError("Consumption amount must be greater than zero.")

    source_balance = get_source_reserved_balance(budget, source_type, str(source_id))
    if amount > source_balance:
        raise ValueError(
            f"Cannot consume {amount}: only {source_balance} reserved for "
            f"source {source_type}/{source_id}."
        )

    consumption = BudgetConsumption.objects.create(
        budget=budget,
        budget_line=None,
        source_type=source_type,
        source_id=str(source_id),
        amount=amount,
        consumption_type=ConsumptionType.CONSUMED,
        status=ConsumptionStatus.APPLIED,
        created_by=consumed_by,
        note=note,
    )
    budget.reserved_amount -= amount
    budget.consumed_amount += amount
    budget.save(update_fields=["reserved_amount", "consumed_amount", "updated_at"])
    return {"status": "consumed", "consumption": consumption}


@transaction.atomic
def release_reserved_budget(
    budget: Budget,
    amount: Decimal,
    source_type: str,
    source_id: str,
    released_by,
    note: str = "",
) -> dict:
    """Header-level release (backward compat)."""
    if budget.status not in (BudgetStatus.ACTIVE, BudgetStatus.EXHAUSTED):
        raise BudgetNotActiveError(
            f"Budget {budget.id} is {budget.status}, cannot release."
        )
    if amount <= 0:
        raise ValueError("Release amount must be greater than zero.")

    source_balance = get_source_reserved_balance(budget, source_type, str(source_id))
    if amount > source_balance:
        raise ValueError(
            f"Cannot release {amount}: only {source_balance} reserved for "
            f"source {source_type}/{source_id}."
        )

    consumption = BudgetConsumption.objects.create(
        budget=budget,
        budget_line=None,
        source_type=source_type,
        source_id=str(source_id),
        amount=amount,
        consumption_type=ConsumptionType.RELEASED,
        status=ConsumptionStatus.APPLIED,
        created_by=released_by,
        note=note,
    )
    budget.reserved_amount -= amount
    budget.save(update_fields=["reserved_amount", "updated_at"])
    return {"status": "released", "consumption": consumption}


# ---------------------------------------------------------------------------
# In-use detection / edit-lock guards — enterprise-grade, history-aware
# ---------------------------------------------------------------------------

def get_budget_in_use_summary(budget: "Budget") -> dict:
    """
    Returns a comprehensive summary of all operational usage on a budget.

    A budget is considered "in-use" if ANY of the following are true,
    regardless of current net balance:
      - has any BudgetConsumption rows (any type, any status applied)
      - has pending variance requests
      - has linked invoice allocations
      - has linked manual expenses
      - has any BudgetLines that are themselves in-use

    This is the authoritative in-use check. It is deliberately conservative:
    even released-only / adjusted-only history counts as "in-use" because it
    proves the budget was operationally referenced.
    """
    # ── Ledger history ───────────────────────────────────────────────────────
    all_rows = BudgetConsumption.objects.filter(budget=budget)
    has_ledger_history = all_rows.exists()

    reserved_total = (
        all_rows.filter(consumption_type=ConsumptionType.RESERVED)
        .aggregate(t=Sum("amount"))["t"] or Decimal("0")
    )
    consumed_total = (
        all_rows.filter(consumption_type=ConsumptionType.CONSUMED)
        .aggregate(t=Sum("amount"))["t"] or Decimal("0")
    )
    released_total = (
        all_rows.filter(consumption_type=ConsumptionType.RELEASED)
        .aggregate(t=Sum("amount"))["t"] or Decimal("0")
    )
    adjusted_total = (
        all_rows.filter(consumption_type=ConsumptionType.ADJUSTED)
        .aggregate(t=Sum("amount"))["t"] or Decimal("0")
    )
    net_reserved = max(reserved_total - released_total - consumed_total, Decimal("0"))

    # ── Variance requests ────────────────────────────────────────────────────
    pending_variance = BudgetVarianceRequest.objects.filter(
        budget=budget, status=VarianceStatus.PENDING
    ).count()
    has_pending_variance = pending_variance > 0

    # ── Cross-module references ─────────────────────────────────────────────
    linked_invoice_allocations_count = 0
    linked_manual_expenses_count = 0
    linked_campaign_count = 0

    try:
        linked_invoice_allocations_count = (
            all_rows.filter(source_type=SourceType.INVOICE_ALLOCATION).count()
        )
    except Exception:
        pass

    try:
        linked_manual_expenses_count = (
            all_rows.filter(source_type=SourceType.MANUAL_EXPENSE).count()
        )
    except Exception:
        pass

    try:
        linked_campaign_count = (
            all_rows.filter(source_type=SourceType.CAMPAIGN).count()
        )
    except Exception:
        pass

    # ── Line-level in-use (recursive check) ─────────────────────────────────
    line_summaries = []
    has_in_use_lines = False
    for line in budget.lines.all():
        line_summary = get_budget_line_in_use_summary(line)
        line_summaries.append(line_summary)
        if line_summary["is_in_use"]:
            has_in_use_lines = True

    # A budget is in-use if it has any history OR pending variance OR in-use lines
    is_in_use = (
        has_ledger_history
        or has_pending_variance
        or has_in_use_lines
        or linked_invoice_allocations_count > 0
        or linked_manual_expenses_count > 0
        or linked_campaign_count > 0
    )

    return {
        # Balance state
        "net_reserved": net_reserved,
        "consumed": consumed_total,
        "released": released_total,
        "adjusted": adjusted_total,
        # Variance
        "pending_variance_requests": pending_variance,
        # Cross-module
        "linked_invoice_allocations_count": linked_invoice_allocations_count,
        "linked_manual_expenses_count": linked_manual_expenses_count,
        "linked_campaign_count": linked_campaign_count,
        # Line breakdown
        "has_in_use_lines": has_in_use_lines,
        "line_summaries": line_summaries,
        # Flags
        "has_ledger_history": has_ledger_history,
        "is_in_use": is_in_use,
    }


def get_budget_line_in_use_summary(line: "BudgetLine") -> dict:
    """
    Returns a comprehensive in-use summary for a single BudgetLine.

    A line is in-use if it has ANY consumption history (not just positive balances),
    OR if any BudgetConsumption rows reference it at all.
    """
    rows = BudgetConsumption.objects.filter(budget_line=line)
    has_ledger_history = rows.exists()

    reserved = rows.filter(consumption_type=ConsumptionType.RESERVED).aggregate(
        t=Sum("amount")
    )["t"] or Decimal("0")
    consumed = rows.filter(consumption_type=ConsumptionType.CONSUMED).aggregate(
        t=Sum("amount")
    )["t"] or Decimal("0")
    released = rows.filter(consumption_type=ConsumptionType.RELEASED).aggregate(
        t=Sum("amount")
    )["t"] or Decimal("0")
    adjusted = rows.filter(consumption_type=ConsumptionType.ADJUSTED).aggregate(
        t=Sum("amount")
    )["t"] or Decimal("0")

    net_reserved = max(reserved - released - consumed, Decimal("0"))

    # Cross-module usage
    invoice_alloc_usage = rows.filter(source_type=SourceType.INVOICE_ALLOCATION).count()
    manual_expense_usage = rows.filter(source_type=SourceType.MANUAL_EXPENSE).count()
    campaign_usage = rows.filter(source_type=SourceType.CAMPAIGN).count()
    variance_usage = BudgetVarianceRequest.objects.filter(
        budget_line=line, status=VarianceStatus.PENDING
    ).count()

    is_in_use = (
        has_ledger_history
        or invoice_alloc_usage > 0
        or manual_expense_usage > 0
        or campaign_usage > 0
        or variance_usage > 0
    )

    return {
        "line_id": line.id,
        "has_ledger_history": has_ledger_history,
        "net_reserved": net_reserved,
        "consumed": consumed,
        "released": released,
        "adjusted": adjusted,
        "invoice_allocation_refs": invoice_alloc_usage,
        "manual_expense_refs": manual_expense_usage,
        "campaign_refs": campaign_usage,
        "pending_variance_requests": variance_usage,
        "is_in_use": is_in_use,
    }


def get_budget_category_in_use_summary(category: "BudgetCategory") -> dict:
    """
    Returns a comprehensive in-use summary for a BudgetCategory.

    A category is in-use if any of its lines have ledger history or active usage.
    """
    lines = BudgetLine.objects.filter(category=category)
    line_summaries = [get_budget_line_in_use_summary(l) for l in lines]

    any_in_use = any(s["is_in_use"] for s in line_summaries)
    has_any_history = any(s["has_ledger_history"] for s in line_summaries)

    return {
        "category_id": category.id,
        "total_lines": lines.count(),
        "has_any_ledger_history": has_any_history,
        "in_use_lines_count": sum(1 for s in line_summaries if s["is_in_use"]),
        "is_in_use": any_in_use,
        "line_summaries": line_summaries,
    }


def get_budget_subcategory_in_use_summary(subcategory: "BudgetSubCategory") -> dict:
    """
    Returns a comprehensive in-use summary for a BudgetSubCategory.
    """
    lines = BudgetLine.objects.filter(subcategory=subcategory)
    line_summaries = [get_budget_line_in_use_summary(l) for l in lines]

    any_in_use = any(s["is_in_use"] for s in line_summaries)
    has_any_history = any(s["has_ledger_history"] for s in line_summaries)

    return {
        "subcategory_id": subcategory.id,
        "total_lines": lines.count(),
        "has_any_ledger_history": has_any_history,
        "in_use_lines_count": sum(1 for s in line_summaries if s["is_in_use"]),
        "is_in_use": any_in_use,
        "line_summaries": line_summaries,
    }


def can_decrease_budget_allocated(budget: "Budget", new_amount: Decimal) -> tuple[bool, str]:
    """
    Returns (True, "") if decreasing allocated_amount to new_amount is safe.
    Returns (False, reason) if it would cut below reserved+consumed floor.
    """
    floor = budget.reserved_amount + budget.consumed_amount
    if new_amount < floor:
        return False, (
            f"Cannot decrease allocated_amount below {floor} "
            f"(reserved={budget.reserved_amount} + consumed={budget.consumed_amount}). "
            f"Release or consume pending reservations first."
        )
    return True, ""


def can_decrease_budget_line_allocated(line: "BudgetLine", new_amount: Decimal) -> tuple[bool, str]:
    """
    Returns (True, "") if decreasing line allocated_amount to new_amount is safe.
    Returns (False, reason) if it would cut below reserved+consumed floor.
    """
    floor = line.reserved_amount + line.consumed_amount
    if new_amount < floor:
        return False, (
            f"Cannot decrease allocated_amount below {floor} "
            f"(reserved={line.reserved_amount} + consumed={line.consumed_amount}). "
            f"Release or consume pending reservations on this line first."
        )
    return True, ""


def can_delete_budget(budget: "Budget") -> tuple[bool, str]:
    """
    Block hard delete if budget has any operational history or active usage.
    Even a budget with only released/adjusted history (net-zero) is protected.
    """
    summary = get_budget_in_use_summary(budget)
    if summary["is_in_use"]:
        reasons = []
        if summary["has_ledger_history"]:
            reasons.append(
                f"has {summary['consumed']} consumed, {summary['net_reserved']} net reserved, "
                f"{summary['released']} released across {summary['consumed']+summary['net_reserved']+summary['released']} ledger rows"
            )
        if summary["pending_variance_requests"] > 0:
            reasons.append(f"{summary['pending_variance_requests']} pending variance request(s)")
        if summary["linked_invoice_allocations_count"] > 0:
            reasons.append(f"{summary['linked_invoice_allocations_count']} invoice allocation(s)")
        if summary["linked_manual_expenses_count"] > 0:
            reasons.append(f"{summary['linked_manual_expenses_count']} manual expense(s)")
        if summary["linked_campaign_count"] > 0:
            reasons.append(f"{summary['linked_campaign_count']} campaign(s)")
        if summary["has_in_use_lines"]:
            reasons.append("in-use budget line(s)")
        return False, (
            f"Cannot delete budget: it is operationally in-use. Reasons: {'; '.join(reasons)}."
        )
    return True, ""


def can_delete_budget_line(line: "BudgetLine") -> tuple[bool, str]:
    """
    Block delete if the line has ANY ledger history (even released-only counts as in-use).
    """
    summary = get_budget_line_in_use_summary(line)
    if summary["is_in_use"]:
        reasons = []
        if summary["has_ledger_history"]:
            reasons.append(
                f"has consumption history: {summary['consumed']} consumed, "
                f"{summary['net_reserved']} net reserved, {summary['released']} released"
            )
        if summary["pending_variance_requests"] > 0:
            reasons.append(f"{summary['pending_variance_requests']} pending variance(s)")
        if summary["invoice_allocation_refs"] > 0:
            reasons.append(f"{summary['invoice_allocation_refs']} invoice allocation(s)")
        if summary["manual_expense_refs"] > 0:
            reasons.append(f"{summary['manual_expense_refs']} manual expense(s)")
        return False, (
            f"Cannot delete budget line: operationally in-use. Reasons: {'; '.join(reasons)}."
        )
    return True, ""


def can_delete_budget_category(category: "BudgetCategory") -> tuple[bool, str]:
    """
    Block delete if any budget line under this category has operational history.
    """
    summary = get_budget_category_in_use_summary(category)
    if summary["is_in_use"]:
        in_use_lines = [s for s in summary["line_summaries"] if s["is_in_use"]]
        return False, (
            f"Cannot delete category '{category.name}': "
            f"{summary['in_use_lines_count']}/{summary['total_lines']} budget line(s) "
            "have operational history or active usage."
        )
    return True, ""


def can_delete_budget_subcategory(subcategory: "BudgetSubCategory") -> tuple[bool, str]:
    """
    Block delete if any budget line under this subcategory has operational history.
    """
    summary = get_budget_subcategory_in_use_summary(subcategory)
    if summary["is_in_use"]:
        return False, (
            f"Cannot delete subcategory '{subcategory.name}': "
            f"{summary['in_use_lines_count']}/{summary['total_lines']} budget line(s) "
            "have operational history or active usage."
        )
    return True, ""


# ---------------------------------------------------------------------------
# Budget import pipeline
# ---------------------------------------------------------------------------

IMPORT_REQUIRED_COLUMNS = {
    "scope_node_code",
    "budget_code",
    "budget_name",
    "financial_year",
    "category_code",
    "allocated_amount",
}

IMPORT_COLUMN_MAP = {
    "scope node code": "scope_node_code",
    "scope_node_code": "scope_node_code",
    "budget code": "budget_code",
    "budget_code": "budget_code",
    "budget name": "budget_name",
    "budget_name": "budget_name",
    "financial year": "financial_year",
    "financial_year": "financial_year",
    "period type": "period_type",
    "period_type": "period_type",
    "period start": "period_start",
    "period_start": "period_start",
    "period end": "period_end",
    "period_end": "period_end",
    "category code": "category_code",
    "category_code": "category_code",
    "subcategory code": "subcategory_code",
    "subcategory_code": "subcategory_code",
    "allocated amount": "allocated_amount",
    "allocated_amount": "allocated_amount",
    "currency": "currency",
}


def parse_budget_import_file(file_obj) -> list[dict]:
    """
    Parse an Excel file (xlsx/xls) and return a list of row dicts.
    The first row is treated as the header. Column names are normalised
    via IMPORT_COLUMN_MAP. Raises ValueError if required columns are missing.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ImportError("openpyxl is required for budget import") from exc

    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
    ws = wb.active

    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        raise ValueError("The uploaded file is empty.")

    headers = []
    for cell in header_row:
        raw = str(cell).strip().lower() if cell is not None else ""
        headers.append(IMPORT_COLUMN_MAP.get(raw, raw))

    missing = IMPORT_REQUIRED_COLUMNS - set(headers)
    if missing:
        raise ValueError(
            f"Excel file is missing required columns: {', '.join(sorted(missing))}. "
            f"Found columns: {', '.join(h for h in headers if h)}."
        )

    parsed = []
    for row_values in rows_iter:
        if all(v is None or str(v).strip() == "" for v in row_values):
            continue  # skip blank rows
        row_dict = {}
        for col_name, cell_value in zip(headers, row_values):
            row_dict[col_name] = str(cell_value).strip() if cell_value is not None else ""
        parsed.append(row_dict)

    wb.close()
    return parsed


def _parse_date(value: str) -> date | None:
    """Try common date formats, return None if unparseable."""
    if not value:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


@transaction.atomic
def create_budget_import_batch(
    org,
    file_name: str,
    parsed_rows: list[dict],
    created_by,
    financial_year: str = "",
    import_mode: str = ImportMode.SAFE_UPDATE,
) -> BudgetImportBatch:
    """
    Create a BudgetImportBatch and BudgetImportRow records from parsed Excel rows.
    Does NOT validate — call validate_budget_import_batch() next.

    Args:
        import_mode: Controls what commit() may do to existing records.
                     SETUP_ONLY  — only create new records; skip existing
                     SAFE_UPDATE — update non-operational records; skip in-use
                     FULL        — update all records (requires explicit intent)
    """
    batch = BudgetImportBatch.objects.create(
        org=org,
        file_name=file_name,
        financial_year=financial_year,
        status=ImportBatchStatus.PENDING,
        import_mode=import_mode,
        total_rows=len(parsed_rows),
        created_by=created_by,
    )
    rows_to_create = []
    for idx, row in enumerate(parsed_rows, start=2):  # 2 = first data row (row 1 is header)
        rows_to_create.append(
            BudgetImportRow(
                batch=batch,
                row_number=idx,
                status=ImportRowStatus.PENDING,
                raw_scope_node_code=row.get("scope_node_code", ""),
                raw_budget_code=row.get("budget_code", ""),
                raw_budget_name=row.get("budget_name", ""),
                raw_financial_year=row.get("financial_year", financial_year),
                raw_period_type=row.get("period_type", "yearly"),
                raw_period_start=row.get("period_start", ""),
                raw_period_end=row.get("period_end", ""),
                raw_category_code=row.get("category_code", ""),
                raw_subcategory_code=row.get("subcategory_code", ""),
                raw_allocated_amount=row.get("allocated_amount", ""),
                raw_currency=row.get("currency", "INR") or "INR",
            )
        )
    BudgetImportRow.objects.bulk_create(rows_to_create)
    return batch


@transaction.atomic
def validate_budget_import_batch(batch: BudgetImportBatch) -> BudgetImportBatch:
    """
    Validate all pending rows in a batch. Resolves FKs and records errors per row.
    Updates batch status to VALIDATED (may have error rows) or FAILED (all rows bad).
    """
    from apps.core.models import ScopeNode

    if batch.status not in (ImportBatchStatus.PENDING, ImportBatchStatus.VALIDATED):
        raise ValueError(f"Batch {batch.id} has status '{batch.status}', cannot re-validate.")

    rows = list(batch.rows.all())
    valid_count = 0
    error_count = 0

    # Cache lookups for performance
    scope_node_cache: dict[str, object] = {}
    category_cache: dict[str, object] = {}
    subcategory_cache: dict[tuple, object] = {}

    for row in rows:
        errors = []

        # --- Scope node ---
        if not row.raw_scope_node_code:
            errors.append("scope_node_code is required.")
            scope_node = None
        else:
            key = row.raw_scope_node_code.lower()
            if key not in scope_node_cache:
                try:
                    scope_node_cache[key] = ScopeNode.objects.get(
                        org=batch.org, code__iexact=row.raw_scope_node_code
                    )
                except ScopeNode.DoesNotExist:
                    scope_node_cache[key] = None
            scope_node = scope_node_cache[key]
            if scope_node is None:
                errors.append(f"scope_node_code '{row.raw_scope_node_code}' not found in org.")

        # --- Budget code/name ---
        if not row.raw_budget_code:
            errors.append("budget_code is required.")
        if not row.raw_budget_name:
            errors.append("budget_name is required.")

        # --- Financial year ---
        financial_year = row.raw_financial_year.strip()
        if not financial_year:
            errors.append("financial_year is required.")

        # --- Period type ---
        period_type_val = row.raw_period_type.lower().strip() if row.raw_period_type else "yearly"
        valid_period_types = [c[0] for c in PeriodType.choices]
        if period_type_val not in valid_period_types:
            errors.append(
                f"period_type '{row.raw_period_type}' is invalid. "
                f"Must be one of: {', '.join(valid_period_types)}."
            )
            period_type_val = None

        # --- Dates ---
        period_start = _parse_date(row.raw_period_start)
        period_end = _parse_date(row.raw_period_end)
        if row.raw_period_start and period_start is None:
            errors.append(f"period_start '{row.raw_period_start}' is not a valid date (use YYYY-MM-DD).")
        if row.raw_period_end and period_end is None:
            errors.append(f"period_end '{row.raw_period_end}' is not a valid date (use YYYY-MM-DD).")
        if period_start and period_end and period_start >= period_end:
            errors.append("period_start must be before period_end.")

        # --- Category ---
        if not row.raw_category_code:
            errors.append("category_code is required.")
            category = None
        else:
            cat_key = row.raw_category_code.lower()
            if cat_key not in category_cache:
                try:
                    category_cache[cat_key] = BudgetCategory.objects.get(
                        org=batch.org, code__iexact=row.raw_category_code
                    )
                except BudgetCategory.DoesNotExist:
                    category_cache[cat_key] = None
            category = category_cache[cat_key]
            if category is None:
                errors.append(f"category_code '{row.raw_category_code}' not found in org.")

        # --- Subcategory (optional) ---
        subcategory = None
        if row.raw_subcategory_code and category:
            sub_key = (category.id, row.raw_subcategory_code.lower())
            if sub_key not in subcategory_cache:
                try:
                    subcategory_cache[sub_key] = BudgetSubCategory.objects.get(
                        category=category, code__iexact=row.raw_subcategory_code
                    )
                except BudgetSubCategory.DoesNotExist:
                    subcategory_cache[sub_key] = None
            subcategory = subcategory_cache[sub_key]
            if subcategory is None:
                errors.append(
                    f"subcategory_code '{row.raw_subcategory_code}' not found "
                    f"under category '{row.raw_category_code}'."
                )

        # --- Allocated amount ---
        allocated = None
        if not row.raw_allocated_amount:
            errors.append("allocated_amount is required.")
        else:
            try:
                allocated = Decimal(row.raw_allocated_amount.replace(",", ""))
                if allocated <= 0:
                    errors.append("allocated_amount must be greater than zero.")
                    allocated = None
            except InvalidOperation:
                errors.append(f"allocated_amount '{row.raw_allocated_amount}' is not a valid number.")

        # --- Resolve existing budget (if any) ---
        resolved_budget = None
        if scope_node and row.raw_budget_code and financial_year:
            try:
                resolved_budget = Budget.objects.get(
                    scope_node=scope_node,
                    code__iexact=row.raw_budget_code,
                    financial_year=financial_year,
                )
            except Budget.DoesNotExist:
                resolved_budget = None  # will be created on commit

        # --- Update row ---
        row.errors = errors
        if errors:
            row.status = ImportRowStatus.ERROR
            row.resolved_scope_node = None
            row.resolved_category = None
            row.resolved_subcategory = None
            row.resolved_budget = None
            row.resolved_budget_line = None
            error_count += 1
        else:
            row.status = ImportRowStatus.VALID
            row.resolved_scope_node = scope_node
            row.resolved_category = category
            row.resolved_subcategory = subcategory
            row.resolved_budget = resolved_budget
            valid_count += 1

        row.save()

    batch.valid_rows = valid_count
    batch.error_rows = error_count
    batch.status = ImportBatchStatus.VALIDATED if valid_count > 0 else ImportBatchStatus.FAILED
    batch.save(update_fields=["valid_rows", "error_rows", "status", "updated_at"])
    return batch


@transaction.atomic
def commit_budget_import_batch(batch: BudgetImportBatch, committed_by) -> BudgetImportBatch:
    """
    Commit all VALID rows according to the batch's import_mode.

    Import mode policy:
      SETUP_ONLY  — Only create new Budget/BudgetLine records.
                    Any row whose budget or line already exists is SKIPPED.
      SAFE_UPDATE — Create new records AND update non-operational existing records.
                    Rows targeting in-use (has ledger history) budgets or lines
                    are SKIPPED with a clear reason.
      FULL       — Create new records AND update ALL existing records.
                    Only use for explicit bulk corrections; use with caution.

    ERROR rows are always skipped (validation failed).
    SKIPPED rows are recorded with a human-readable skipped_reason.
    """
    if batch.status != ImportBatchStatus.VALIDATED:
        raise ValueError(
            f"Batch {batch.id} has status '{batch.status}', expected VALIDATED."
        )
    if batch.valid_rows == 0:
        raise ValueError("No valid rows to commit.")

    import_mode = batch.import_mode
    valid_rows = list(batch.rows.filter(status=ImportRowStatus.VALID))
    committed_count = 0
    skipped_count = 0

    for row in valid_rows:
        # Determine parsed values
        financial_year = row.raw_financial_year.strip()
        period_type_val = row.raw_period_type.lower().strip() if row.raw_period_type else "yearly"
        period_start = _parse_date(row.raw_period_start)
        period_end = _parse_date(row.raw_period_end)
        allocated = Decimal(row.raw_allocated_amount.replace(",", ""))
        currency = row.raw_currency.upper() if row.raw_currency else "INR"

        # ── Resolve or create Budget ─────────────────────────────────────────
        budget_existed = False
        if row.resolved_scope_node and row.raw_budget_code and financial_year:
            existing = Budget.objects.filter(
                scope_node=row.resolved_scope_node,
                code__iexact=row.raw_budget_code,
                financial_year=financial_year,
            ).first()
            budget_existed = existing is not None

        if budget_existed:
            # ── Budget already exists — apply import mode policy ─────────────
            budget = existing

            if import_mode == ImportMode.SETUP_ONLY:
                row.skipped_reason = (
                    f"Budget '{row.raw_budget_code}' already exists "
                    f"(scope={row.raw_scope_node_code}, year={financial_year}). "
                    f"Import mode is SETUP_ONLY: existing records are not modified."
                )
                row.status = ImportRowStatus.SKIPPED
                row.save()
                skipped_count += 1
                continue

            # For SAFE_UPDATE or FULL: check budget-level in-use
            if import_mode == ImportMode.SAFE_UPDATE:
                budget_summary = get_budget_in_use_summary(budget)
                if not budget_summary["is_in_use"]:
                    # SAFE_UPDATE: allowed to update non-in-use budget header fields only.
                    pass
                else:
                    # In-use budgets are still allowed to receive new lines. We only freeze
                    # destructive/mutating updates to the existing operational structure.
                    pass
            # FULL: always allowed to proceed

            # Update mutable header fields (never financial amounts at header level)
            updated_fields = []
            budget_in_use = get_budget_in_use_summary(budget)["is_in_use"] if import_mode == ImportMode.SAFE_UPDATE else False
            if not budget_in_use and period_type_val and budget.period_type != period_type_val:
                budget.period_type = period_type_val
                updated_fields.append("period_type")
            if not budget_in_use and period_start and budget.period_start != period_start:
                budget.period_start = period_start
                updated_fields.append("period_start")
            if not budget_in_use and period_end and budget.period_end != period_end:
                budget.period_end = period_end
                updated_fields.append("period_end")
            if updated_fields:
                updated_fields.append("updated_at")
                budget.save(update_fields=updated_fields)

        else:
            # Budget does not exist — create it (always allowed regardless of mode)
            budget = Budget.objects.create(
                org=batch.org,
                scope_node=row.resolved_scope_node,
                name=row.raw_budget_name,
                code=row.raw_budget_code,
                financial_year=financial_year,
                period_type=period_type_val,
                period_start=period_start,
                period_end=period_end,
                currency=currency,
                status=BudgetStatus.DRAFT,
                created_by=committed_by,
            )

        # ── Resolve or create BudgetLine ─────────────────────────────────────
        line_existed = False
        if row.resolved_category:
            if row.resolved_subcategory:
                existing_line = BudgetLine.objects.filter(
                    budget=budget,
                    category=row.resolved_category,
                    subcategory=row.resolved_subcategory,
                ).first()
            else:
                existing_line = BudgetLine.objects.filter(
                    budget=budget,
                    category=row.resolved_category,
                    subcategory__isnull=True,
                ).first()
            line_existed = existing_line is not None

        if line_existed:
            line = existing_line

            if import_mode == ImportMode.SETUP_ONLY:
                row.skipped_reason = (
                    f"BudgetLine already exists (budget={budget.code}, "
                    f"category={row.raw_category_code}, subcategory={row.raw_subcategory_code or 'none'}). "
                    f"Import mode is SETUP_ONLY: existing lines are not modified."
                )
                row.status = ImportRowStatus.SKIPPED
                row.save()
                skipped_count += 1
                continue

            # For SAFE_UPDATE: check line-level in-use
            if import_mode == ImportMode.SAFE_UPDATE:
                line_summary = get_budget_line_in_use_summary(line)
                if line_summary["is_in_use"]:
                    row.skipped_reason = (
                        f"BudgetLine id={line.id} is operationally in-use "
                        f"(has history={line_summary['has_ledger_history']}, "
                        f"net reserved={line_summary['net_reserved']}, "
                        f"pending variance={line_summary['pending_variance_requests']}). "
                        f"Import mode is SAFE_UPDATE: in-use lines are skipped."
                    )
                    row.status = ImportRowStatus.SKIPPED
                    row.save()
                    skipped_count += 1
                    continue
            # Determine allowed allocation change
            if allocated != line.allocated_amount:
                if import_mode == ImportMode.SAFE_UPDATE:
                    # Only allow allocation increase; decreases are blocked
                    ok, reason = can_decrease_budget_line_allocated(line, allocated)
                    if not ok:
                        row.skipped_reason = (
                            f"Import wants to set allocated={allocated} but line currently has "
                            f"allocated={line.allocated_amount}. "
                            f"SAFE_UPDATE: decreases blocked ({reason})"
                        )
                        row.status = ImportRowStatus.SKIPPED
                        row.save()
                        skipped_count += 1
                        continue
                    if allocated > line.allocated_amount:
                        line.allocated_amount = allocated
                        line.save(update_fields=["allocated_amount", "updated_at"])
                    # If equal, nothing to do
                elif import_mode == ImportMode.FULL:
                    # Allow any change, but enforce floor guard
                    ok, reason = can_decrease_budget_line_allocated(line, allocated)
                    if not ok:
                        row.skipped_reason = (
                            f"Import change would violate floor guard: {reason}"
                        )
                        row.status = ImportRowStatus.SKIPPED
                        row.save()
                        skipped_count += 1
                        continue
                    line.allocated_amount = allocated
                    line.save(update_fields=["allocated_amount", "updated_at"])

        else:
            # Line does not exist — create it (always allowed)
            line = BudgetLine.objects.create(
                budget=budget,
                category=row.resolved_category,
                subcategory=row.resolved_subcategory,
                allocated_amount=allocated,
            )

        # Sync budget header allocated_amount = sum of lines
        from django.db.models import Sum as DSum
        lines_total = budget.lines.aggregate(t=DSum("allocated_amount"))["t"] or Decimal("0")
        if budget.allocated_amount != lines_total:
            budget.allocated_amount = lines_total
            budget.save(update_fields=["allocated_amount", "updated_at"])

        row.resolved_budget = budget
        row.resolved_budget_line = line
        row.status = ImportRowStatus.COMMITTED
        row.save()
        committed_count += 1

    batch.committed_rows = committed_count
    batch.skipped_rows = skipped_count
    batch.status = ImportBatchStatus.COMMITTED
    batch.committed_by = committed_by
    batch.committed_at = timezone.now()
    batch.save(update_fields=["committed_rows", "skipped_rows", "status", "committed_by", "committed_at", "updated_at"])
    return batch
