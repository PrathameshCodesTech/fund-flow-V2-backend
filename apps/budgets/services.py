from django.db import transaction
from django.utils import timezone
from decimal import Decimal

from apps.budgets.models import (
    Budget,
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


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------

def calculate_projected_utilization(budget: Budget, amount: Decimal):
    """
    Returns (current_utilization_percent, projected_utilization_percent).

    Both are expressed as percentages of allocated_amount.
    """
    if budget.allocated_amount <= 0:
        current = Decimal("0")
    else:
        current = ((budget.reserved_amount + budget.consumed_amount) / budget.allocated_amount) * 100

    if budget.allocated_amount <= 0:
        projected = Decimal("0")
    else:
        projected = ((budget.reserved_amount + budget.consumed_amount + amount) / budget.allocated_amount) * 100

    return current, projected


def get_source_reserved_balance(budget: Budget, source_type: str, source_id: str) -> Decimal:
    """
    Returns the net reserved balance for a specific (budget, source_type, source_id) tuple.

    Net = sum(RESERVED) - sum(CONSUMED) - sum(RELEASED) across all applied rows.
    This prevents one source from consuming/releasing amounts reserved by another.
    """
    from django.db.models import Sum, Q
    rows = BudgetConsumption.objects.filter(
        budget=budget,
        source_type=source_type,
        source_id=str(source_id),
        status=ConsumptionStatus.APPLIED,
    )
    reserved = rows.filter(consumption_type=ConsumptionType.RESERVED).aggregate(
        t=Sum("amount")
    )["t"] or Decimal("0")
    consumed = rows.filter(consumption_type=ConsumptionType.CONSUMED).aggregate(
        t=Sum("amount")
    )["t"] or Decimal("0")
    released = rows.filter(consumption_type=ConsumptionType.RELEASED).aggregate(
        t=Sum("amount")
    )["t"] or Decimal("0")
    return reserved - consumed - released


def _get_rule(budget: Budget) -> BudgetRule | None:
    """Return the active rule for a budget, or None for defaults."""
    try:
        return budget.rule if budget.rule.is_active else None
    except BudgetRule.DoesNotExist:
        return None


# ---------------------------------------------------------------------------
# Reserve budget
# ---------------------------------------------------------------------------

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
    Attempt to reserve `amount` from `budget`.

    Returns a dict with keys:
        status: "reserved" | "reserved_with_warning" | "variance_required"
        consumption: BudgetConsumption | None
        variance_request: BudgetVarianceRequest | None
        projected_utilization: Decimal
        current_utilization: Decimal

    Raises:
        BudgetNotActiveError — if budget.status != ACTIVE
        BudgetLimitExceeded — if projected > hard_block_threshold
        ValueError — if amount <= 0
    """
    if budget.status != BudgetStatus.ACTIVE:
        raise BudgetNotActiveError(
            f"Budget {budget.id} is {budget.status}, expected ACTIVE."
        )

    # Always read fresh from DB so we don't use a stale in-memory object
    budget.refresh_from_db()

    if amount <= 0:
        raise ValueError("Reservation amount must be greater than zero.")

    # Get rule or use defaults
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

    # Hard block
    if projected_util >= hard_block:
        raise BudgetLimitExceeded(
            f"Reservation of {amount} would bring projected utilization to "
            f"{projected_util:.2f}%, exceeding hard block threshold of "
            f"{hard_block:.2f}%. Variance approval required."
        )

    consumption = None
    variance_req = None

    if projected_util >= approval:
        # Approval required — create pending variance, do NOT reserve
        variance_req = BudgetVarianceRequest.objects.create(
            budget=budget,
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

    # Apply reservation
    consumption = BudgetConsumption.objects.create(
        budget=budget,
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


# ---------------------------------------------------------------------------
# Consume reserved budget
# ---------------------------------------------------------------------------

@transaction.atomic
def consume_reserved_budget(
    budget: Budget,
    amount: Decimal,
    source_type: str,
    source_id: str,
    consumed_by,
    note: str = "",
) -> dict:
    """
    Convert a portion (or all) of a reserved amount into consumed.

    Rules:
        - amount must be > 0
        - cannot consume more than budget.reserved_amount
        - creates BudgetConsumption type=consumed
        - reduces reserved_amount, increases consumed_amount

    Returns dict with status and consumption record.
    """
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

    return {
        "status": "consumed",
        "consumption": consumption,
    }


# ---------------------------------------------------------------------------
# Release reserved budget
# ---------------------------------------------------------------------------

@transaction.atomic
def release_reserved_budget(
    budget: Budget,
    amount: Decimal,
    source_type: str,
    source_id: str,
    released_by,
    note: str = "",
) -> dict:
    """
    Release a previously reserved amount back to available pool.

    Rules:
        - amount must be > 0
        - cannot release more than budget.reserved_amount
        - creates BudgetConsumption type=released
        - reduces reserved_amount

    Returns dict with status and consumption record.
    """
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
    decision: str,  # "approved" or "rejected"
    reviewed_by,
    review_note: str = "",
) -> BudgetVarianceRequest:
    """
    Approve or reject a pending variance request.

    If approved:
        - creates BudgetConsumption type=reserved status=applied
        - increments budget.reserved_amount
        - sets variance_request status to APPROVED

    If rejected:
        - no budget changes
        - sets variance_request status to REJECTED

    Only PENDING requests can be reviewed.
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

        # Create the reservation that was pending approval
        BudgetConsumption.objects.create(
            budget=variance_request.budget,
            source_type=variance_request.source_type,
            source_id=str(variance_request.source_id),
            amount=variance_request.requested_amount,
            consumption_type=ConsumptionType.RESERVED,
            status=ConsumptionStatus.APPLIED,
            created_by=reviewed_by,
            note=f"Approved variance request {variance_request.id}: {review_note}",
        )
        variance_request.budget.reserved_amount += variance_request.requested_amount
        variance_request.budget.save(update_fields=["reserved_amount", "updated_at"])

    else:
        variance_request.status = VarianceStatus.REJECTED
        variance_request.save()

    return variance_request
