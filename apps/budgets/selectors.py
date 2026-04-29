"""
Budget analytics selectors for the overview dashboard.

Ledger truth policy:
  - allocated_amount is authoritative (set by user import or direct create)
  - reserved_amount and consumed_amount are denormalised performance caches;
    they may drift from ledger truth after bulk corrections or direct DB edits
  - All reporting aggregations use ledger-derived balances (BudgetConsumption rows)
    for reserved/consumed/available to ensure consistency
  - The Budget model's available_amount and utilization_percent remain available
    as convenience views but reporting selectors use ledger-derived values
"""
from django.db.models import Sum, Count, Q, F, Value, DecimalField
from django.db.models.functions import Coalesce
from decimal import Decimal
from django.db.models import Sum as DSum

from apps.budgets.models import (
    Budget, BudgetLine, BudgetConsumption,
    ConsumptionType, ConsumptionStatus
)
from apps.campaigns.models import Campaign
from apps.core.models import ScopeNode
from apps.access.selectors import get_user_visible_scope_ids


def _ledger_reserved_for_budgets(budget_ids):
    """
    Return dict: {budget_id: ledger_reserved_decimal}
    ledger_reserved = sum(RESERVED) - sum(RELEASED) - sum(CONSUMED) for APPLIED rows.
    """
    if not budget_ids:
        return {}
    rows = (
        BudgetConsumption.objects
        .filter(budget_id__in=budget_ids, status=ConsumptionStatus.APPLIED)
        .values("budget_id")
        .annotate(
            reserved=Coalesce(Sum("amount", filter=Q(consumption_type=ConsumptionType.RESERVED)), Value(Decimal("0"))),
            consumed=Coalesce(Sum("amount", filter=Q(consumption_type=ConsumptionType.CONSUMED)), Value(Decimal("0"))),
            released=Coalesce(Sum("amount", filter=Q(consumption_type=ConsumptionType.RELEASED)), Value(Decimal("0"))),
        )
    )
    return {
        r["budget_id"]: max(r["reserved"] - r["released"] - r["consumed"], Decimal("0"))
        for r in rows
    }


def _ledger_consumed_for_budgets(budget_ids):
    """
    Return dict: {budget_id: ledger_consumed_decimal}
    ledger_consumed = sum(CONSUMED) for APPLIED rows.
    """
    if not budget_ids:
        return {}
    rows = (
        BudgetConsumption.objects
        .filter(budget_id__in=budget_ids, status=ConsumptionStatus.APPLIED)
        .values("budget_id")
        .annotate(consumed=Coalesce(Sum("amount", filter=Q(consumption_type=ConsumptionType.CONSUMED)), Value(Decimal("0"))))
    )
    return {r["budget_id"]: r["consumed"] for r in rows}


def _ledger_balances_for_budgets(budget_ids):
    """
    Return dict: {budget_id: {"reserved": Decimal, "consumed": Decimal, "available": Decimal}}
    Uses ledger exclusively — authoritative source for reporting.
    """
    if not budget_ids:
        return {}
    reserved_map = _ledger_reserved_for_budgets(budget_ids)
    consumed_map = _ledger_consumed_for_budgets(budget_ids)

    budgets = {b.id: b for b in Budget.objects.filter(id__in=budget_ids).select_related("scope_node")}
    result = {}
    for bid in budget_ids:
        allocated = budgets[bid].allocated_amount if bid in budgets else Decimal("0")
        reserved = reserved_map.get(bid, Decimal("0"))
        consumed = consumed_map.get(bid, Decimal("0"))
        available = max(allocated - reserved - consumed, Decimal("0"))
        result[bid] = {"reserved": reserved, "consumed": consumed, "available": available}
    return result


def _ledger_balances_for_lines(line_ids):
    """
    Return dict: {line_id: {"reserved": Decimal, "consumed": Decimal, "available": Decimal}}
    using ledger rows per BudgetLine.
    """
    if not line_ids:
        return {}
    rows = (
        BudgetConsumption.objects
        .filter(budget_line_id__in=line_ids, status=ConsumptionStatus.APPLIED)
        .values("budget_line_id")
        .annotate(
            reserved=Coalesce(Sum("amount", filter=Q(consumption_type=ConsumptionType.RESERVED)), Value(Decimal("0"))),
            consumed=Coalesce(Sum("amount", filter=Q(consumption_type=ConsumptionType.CONSUMED)), Value(Decimal("0"))),
            released=Coalesce(Sum("amount", filter=Q(consumption_type=ConsumptionType.RELEASED)), Value(Decimal("0"))),
        )
    )
    line_map = {l.id: l for l in BudgetLine.objects.filter(id__in=line_ids)}
    result = {}
    raw = {r["budget_line_id"]: r for r in rows}
    for line_id in line_ids:
        line = line_map[line_id]
        row = raw.get(line_id, {})
        reserved = row.get("reserved", Decimal("0"))
        consumed = row.get("consumed", Decimal("0"))
        released = row.get("released", Decimal("0"))
        net_reserved = max(reserved - released - consumed, Decimal("0"))
        result[line_id] = {
            "reserved": net_reserved,
            "consumed": consumed,
            "available": max(line.allocated_amount - net_reserved - consumed, Decimal("0")),
        }
    return result


def get_budget_live_balances(budget: "Budget") -> dict:
    """
    Return real-time balances for a budget header by aggregating the ledger.
    Ledger is authoritative; denormalised fields are not used here.
    """
    budget_ids = [budget.id]
    balances = _ledger_balances_for_budgets(budget_ids)
    ledger = balances.get(budget.id, {"reserved": Decimal("0"), "consumed": Decimal("0"), "available": Decimal("0")})

    lines = []
    for line in budget.lines.select_related("category", "subcategory").all():
        line_balances = get_budget_line_live_balances(line)
        lines.append({
            "id": line.id,
            "category_id": line.category_id,
            "category_name": line.category.name,
            "subcategory_id": line.subcategory_id,
            "subcategory_name": line.subcategory.name if line.subcategory else None,
            "allocated_amount": str(line.allocated_amount),
            **{k: str(v) for k, v in line_balances.items()},
        })

    return {
        "budget_id": budget.id,
        "allocated_amount": str(budget.allocated_amount),
        "reserved_amount": str(ledger["reserved"]),
        "consumed_amount": str(ledger["consumed"]),
        "available_amount": str(ledger["available"]),
        "utilization_percent": str(
            round((ledger["reserved"] + ledger["consumed"]) / budget.allocated_amount * 100, 2)
            if budget.allocated_amount else Decimal("0")
        ),
        "lines": lines,
    }


def get_budget_line_live_balances(line: "BudgetLine") -> dict:
    """Return real-time balances for a single BudgetLine from the ledger."""
    rows = BudgetConsumption.objects.filter(
        budget_line=line, status=ConsumptionStatus.APPLIED
    )
    reserved = rows.filter(consumption_type=ConsumptionType.RESERVED).aggregate(
        t=Coalesce(Sum("amount"), Value(Decimal("0")))
    )["t"]
    consumed = rows.filter(consumption_type=ConsumptionType.CONSUMED).aggregate(
        t=Coalesce(Sum("amount"), Value(Decimal("0")))
    )["t"]
    released = rows.filter(consumption_type=ConsumptionType.RELEASED).aggregate(
        t=Coalesce(Sum("amount"), Value(Decimal("0")))
    )["t"]
    net_reserved = max(reserved - released - consumed, Decimal("0"))
    available = max(line.allocated_amount - net_reserved - consumed, Decimal("0"))
    return {
        "reserved_amount": net_reserved,
        "consumed_amount": consumed,
        "available_amount": available,
    }


def get_budgets_overview(user):
    """
    Aggregate budget data for the enterprise marketing budget control dashboard.
    Returns all data scoped to user-visible scope nodes.

    Ledger truth policy: all balance figures (reserved/consumed/available) are
    derived from BudgetConsumption ledger rows. allocated_amount comes from
    the Budget model (authoritative source for allocation).

    Performance note: for very large datasets, consider adding a periodic
    cache-update signal on BudgetConsumption save/delete to keep denormalised
    counters in sync, and fall back to those when the ledger query is too slow.
    At present, ledger queries are used directly.
    """
    visible = get_user_visible_scope_ids(user)

    # ── Budget queryset ──────────────────────────────────────────────────────
    budget_qs = Budget.objects.filter(scope_node_id__in=visible)
    budget_ids = list(budget_qs.values_list("id", flat=True))

    # Ledger-derived balances for all visible budgets (single query)
    all_ledger = _ledger_balances_for_budgets(budget_ids)

    # ── Summary totals ──────────────────────────────────────────────────────
    total_allocated = sum((b.allocated_amount for b in budget_qs.select_related("scope_node")), Decimal("0"))
    total_reserved = sum((all_ledger.get(b.id, {}).get("reserved", Decimal("0")) for b in budget_qs), Decimal("0"))
    total_consumed = sum((all_ledger.get(b.id, {}).get("consumed", Decimal("0")) for b in budget_qs), Decimal("0"))
    total_available = sum((all_ledger.get(b.id, {}).get("available", Decimal("0")) for b in budget_qs), Decimal("0"))

    regions = list(
        ScopeNode.objects.filter(node_type="region", id__in=visible)
        .values("id", "name")
        .order_by("name")
    )
    region_ids = [r["id"] for r in regions]
    parks_count = ScopeNode.objects.filter(
        node_type="cost_center", parent_id__in=region_ids
    ).count()
    campaigns_count = Campaign.objects.filter(
        scope_node_id__in=visible, status="internally_approved"
    ).count()

    # Line queryset scoped to user-visible budgets
    line_qs = BudgetLine.objects.filter(budget__scope_node_id__in=visible)

    # ── By Region ────────────────────────────────────────────────────────────
    region_data = {}
    for region in regions:
        region_parks = list(
            ScopeNode.objects.filter(parent_id=region["id"], node_type="cost_center")
            .values_list("id", flat=True)
        )
        park_ids_in_region = [p for p in region_parks if p in visible]
        park_budgets_qs = budget_qs.filter(scope_node_id__in=park_ids_in_region)
        park_budget_ids = list(park_budgets_qs.values_list("id", flat=True))
        park_ledger = {
            bid: all_ledger.get(bid, {"reserved": Decimal("0"), "consumed": Decimal("0")})
            for bid in park_budget_ids
        }
        alloc = sum((b.allocated_amount for b in park_budgets_qs), Decimal("0"))
        reserv = sum((park_ledger.get(bid, {}).get("reserved", Decimal("0")) for bid in park_budget_ids), Decimal("0"))
        cons = sum((park_ledger.get(bid, {}).get("consumed", Decimal("0")) for bid in park_budget_ids), Decimal("0"))
        avail = sum((park_ledger.get(bid, {}).get("available", Decimal("0")) for bid in park_budget_ids), Decimal("0"))
        util_pct = round((reserv + cons) / alloc * 100, 1) if alloc else 0
        region_data[region["id"]] = {
            "id": region["id"],
            "name": region["name"],
            "allocated_amount": str(alloc),
            "reserved_amount": str(reserv),
            "consumed_amount": str(cons),
            "available_amount": str(avail),
            "utilization_percent": util_pct,
            "parks_count": len(park_ids_in_region),
            "budgets_count": park_budgets_qs.count(),
        }

    # ── Parks ──────────────────────────────────────────────────────────────
    parks_data = []
    all_parks = (
        ScopeNode.objects
        .filter(node_type="cost_center", parent_id__in=region_ids)
        .select_related("parent")
        .order_by("parent__name", "name")
    )
    for park in all_parks:
        if park.id not in visible:
            continue
        park_budgets_qs = budget_qs.filter(scope_node_id=park.id)
        park_budget_ids = list(park_budgets_qs.values_list("id", flat=True))
        park_ledger = {
            bid: all_ledger.get(bid, {"reserved": Decimal("0"), "consumed": Decimal("0")})
            for bid in park_budget_ids
        }
        alloc = sum((b.allocated_amount for b in park_budgets_qs), Decimal("0"))
        reserv = sum((park_ledger.get(bid, {}).get("reserved", Decimal("0")) for bid in park_budget_ids), Decimal("0"))
        cons = sum((park_ledger.get(bid, {}).get("consumed", Decimal("0")) for bid in park_budget_ids), Decimal("0"))
        avail = sum((park_ledger.get(bid, {}).get("available", Decimal("0")) for bid in park_budget_ids), Decimal("0"))
        util_pct = round((reserv + cons) / alloc * 100, 1) if alloc else 0

        top_subcats = []
        park_line_qs = line_qs.filter(budget__scope_node_id=park.id)
        if park_line_qs.filter(subcategory__isnull=False).exists():
            subcat_agg = (
                park_line_qs
                .filter(subcategory__isnull=False)
                .values("subcategory__id", "subcategory__name")
                .annotate(total=Sum("allocated_amount"))
                .order_by("-total")[:5]
            )
            top_subcats = [
                {"id": r["subcategory__id"], "name": r["subcategory__name"], "amount": str(r["total"])}
                for r in subcat_agg
            ]

        parks_data.append({
            "id": park.id,
            "region_id": park.parent_id,
            "region_name": park.parent.name if park.parent else "",
            "name": park.name,
            "allocated_amount": str(alloc),
            "reserved_amount": str(reserv),
            "consumed_amount": str(cons),
            "available_amount": str(avail),
            "utilization_percent": util_pct,
            "budgets_count": park_budgets_qs.count(),
            "top_subcategories": top_subcats,
        })

    # ── By Category (from BudgetLine) ────────────────────────────────────────
    cat_data = []
    visible_line_ids = list(line_qs.values_list("id", flat=True))
    line_ledger = _ledger_balances_for_lines(visible_line_ids)

    category_qs = (
        line_qs
        .values("category__id", "category__name")
        .annotate(
            allocated_amount=Sum("allocated_amount"),
            budgets_count=Count("budget", distinct=True),
        )
        .order_by("-allocated_amount")
    )
    for row in category_qs:
        category_line_ids = list(
            line_qs.filter(category_id=row["category__id"]).values_list("id", flat=True)
        )
        reserved = sum((line_ledger.get(lid, {}).get("reserved", Decimal("0")) for lid in category_line_ids), Decimal("0"))
        consumed = sum((line_ledger.get(lid, {}).get("consumed", Decimal("0")) for lid in category_line_ids), Decimal("0"))
        available = sum((line_ledger.get(lid, {}).get("available", Decimal("0")) for lid in category_line_ids), Decimal("0"))
        campaigns = Campaign.objects.filter(
            scope_node_id__in=visible,
            category_id=row["category__id"],
            status="internally_approved",
        ).count()
        cat_data.append({
            "id": row["category__id"],
            "name": row["category__name"] or "—",
            "allocated_amount": str(row["allocated_amount"] or 0),
            "reserved_amount": str(reserved),
            "consumed_amount": str(consumed),
            "available_amount": str(available),
            "budgets_count": row["budgets_count"],
            "campaigns_count": campaigns,
        })

    # ── By Subcategory (from BudgetLine) ────────────────────────────────────
    subcat_data = []
    subcat_qs = (
        line_qs
        .filter(subcategory__isnull=False)
        .values("subcategory__id", "subcategory__name", "category__name")
        .annotate(allocated_amount=Sum("allocated_amount"))
        .order_by("-allocated_amount")[:50]
    )
    for row in subcat_qs:
        subcategory_line_ids = list(
            line_qs.filter(subcategory_id=row["subcategory__id"]).values_list("id", flat=True)
        )
        reserved = sum((line_ledger.get(lid, {}).get("reserved", Decimal("0")) for lid in subcategory_line_ids), Decimal("0"))
        consumed = sum((line_ledger.get(lid, {}).get("consumed", Decimal("0")) for lid in subcategory_line_ids), Decimal("0"))
        available = sum((line_ledger.get(lid, {}).get("available", Decimal("0")) for lid in subcategory_line_ids), Decimal("0"))
        subcat_data.append({
            "id": row["subcategory__id"],
            "name": row["subcategory__name"] or "—",
            "category_name": row["category__name"] or "—",
            "allocated_amount": str(row["allocated_amount"] or 0),
            "reserved_amount": str(reserved),
            "consumed_amount": str(consumed),
            "available_amount": str(available),
        })

    # ── Top Campaigns ───────────────────────────────────────────────────────
    campaign_data = []
    campaigns_qs = (
        Campaign.objects
        .filter(scope_node_id__in=visible, status="internally_approved")
        .select_related("category", "subcategory")
        .order_by("-approved_amount")[:50]
    )
    for c in campaigns_qs:
        scope = None
        try:
            scope = ScopeNode.objects.get(id=c.scope_node_id)
        except ScopeNode.DoesNotExist:
            scope = None
        region_name = ""
        if scope and scope.parent_id:
            try:
                region = ScopeNode.objects.get(id=scope.parent_id)
                region_name = region.name
            except ScopeNode.DoesNotExist:
                region_name = ""
        campaign_data.append({
            "id": c.id,
            "name": c.name,
            "scope_node_name": scope.name if scope else "—",
            "region_name": region_name,
            "category_name": c.category.name if c.category else "—",
            "subcategory_name": c.subcategory.name if c.subcategory else "—",
            "approved_amount": str(c.approved_amount or 0),
            "status": c.status,
        })

    return {
        "summary": {
            "total_allocated": str(total_allocated),
            "total_reserved": str(total_reserved),
            "total_consumed": str(total_consumed),
            "total_available": str(total_available),
            "regions_count": len(regions),
            "parks_count": parks_count,
            "campaigns_count": campaigns_count,
            "budgets_count": len(budget_ids),
        },
        "regions": [region_data[r["id"]] for r in regions],
        "parks": parks_data,
        "categories": cat_data,
        "subcategories": subcat_data,
        "campaigns": campaign_data,
    }
