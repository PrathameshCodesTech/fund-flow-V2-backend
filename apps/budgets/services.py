from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from decimal import Decimal

from apps.budgets.models import (
    Budget,
    BudgetLine,
    BudgetRule,
    BudgetConsumption,
    BudgetVarianceRequest,
    ConsumptionType,
    ConsumptionStatus,
    VarianceStatus,
    BudgetStatus,
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
