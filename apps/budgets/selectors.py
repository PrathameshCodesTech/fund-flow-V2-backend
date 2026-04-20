"""
Budget analytics selectors for the overview dashboard.
"""
from django.db.models import Sum, Count

from apps.budgets.models import Budget
from apps.campaigns.models import Campaign
from apps.core.models import ScopeNode
from apps.access.selectors import get_user_visible_scope_ids


def get_budgets_overview(user):
    """
    Aggregate budget data for the enterprise marketing budget control dashboard.
    Returns all data scoped to user-visible scope nodes (parks/cost centers).
    """
    visible = get_user_visible_scope_ids(user)

    # ── Summary totals ──────────────────────────────────────────────────────
    budget_qs = Budget.objects.filter(scope_node_id__in=visible)
    summary = budget_qs.aggregate(
        total_allocated=Sum("allocated_amount"),
        total_reserved=Sum("reserved_amount"),
        total_consumed=Sum("consumed_amount"),
    )
    allocated = summary["total_allocated"] or 0
    reserved = summary["total_reserved"] or 0
    consumed = summary["total_consumed"] or 0

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

    # ── By Region ────────────────────────────────────────────────────────────
    region_data = {}
    for region in regions:
        region_parks = list(
            ScopeNode.objects.filter(parent_id=region["id"], node_type="cost_center")
            .values_list("id", flat=True)
        )
        park_ids_in_region = [p for p in region_parks if p in visible]
        park_budgets = budget_qs.filter(scope_node_id__in=park_ids_in_region)
        agg = park_budgets.aggregate(
            allocated=Sum("allocated_amount"),
            reserved=Sum("reserved_amount"),
            consumed=Sum("consumed_amount"),
        )
        alloc = agg["allocated"] or 0
        reserv = agg["reserved"] or 0
        cons = agg["consumed"] or 0
        available = alloc - reserv - cons
        util_pct = round((reserv + cons) / alloc * 100, 1) if alloc else 0
        region_data[region["id"]] = {
            "id": region["id"],
            "name": region["name"],
            "allocated_amount": str(alloc),
            "reserved_amount": str(reserv),
            "consumed_amount": str(cons),
            "available_amount": str(available),
            "utilization_percent": util_pct,
            "parks_count": len(park_ids_in_region),
            "budgets_count": park_budgets.count(),
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
        park_budgets = budget_qs.filter(scope_node_id=park.id)
        agg = park_budgets.aggregate(
            allocated=Sum("allocated_amount"),
            reserved=Sum("reserved_amount"),
            consumed=Sum("consumed_amount"),
        )
        alloc = agg["allocated"] or 0
        reserv = agg["reserved"] or 0
        cons = agg["consumed"] or 0
        available = alloc - reserv - cons
        util_pct = round((reserv + cons) / alloc * 100, 1) if alloc else 0

        top_subcats = []
        if park_budgets.exists():
            subcat_agg = (
                park_budgets
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
            "available_amount": str(available),
            "utilization_percent": util_pct,
            "budgets_count": park_budgets.count(),
            "top_subcategories": top_subcats,
        })

    # ── By Category ─────────────────────────────────────────────────────────
    cat_data = []
    category_qs = (
        Budget.objects
        .filter(scope_node_id__in=visible, category__isnull=False)
        .values("category__id", "category__name")
        .annotate(
            allocated_amount=Sum("allocated_amount"),
            budgets_count=Count("id"),
        )
        .order_by("-allocated_amount")
    )
    for row in category_qs:
        campaigns = Campaign.objects.filter(
            scope_node_id__in=visible,
            category_id=row["category__id"],
            status="internally_approved",
        ).count()
        cat_data.append({
            "id": row["category__id"],
            "name": row["category__name"] or "—",
            "allocated_amount": str(row["allocated_amount"] or 0),
            "budgets_count": row["budgets_count"],
            "campaigns_count": campaigns,
        })

    # ── By Subcategory ──────────────────────────────────────────────────────
    subcat_data = []
    subcat_qs = (
        Budget.objects
        .filter(scope_node_id__in=visible, subcategory__isnull=False)
        .select_related("category")
        .values("subcategory__id", "subcategory__name", "category__name")
        .annotate(allocated_amount=Sum("allocated_amount"))
        .order_by("-allocated_amount")[:50]
    )
    for row in subcat_qs:
        subcat_data.append({
            "id": row["subcategory__id"],
            "name": row["subcategory__name"] or "—",
            "category_name": row["category__name"] or "—",
            "allocated_amount": str(row["allocated_amount"] or 0),
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
            "total_allocated": str(allocated),
            "total_reserved": str(reserved),
            "total_consumed": str(consumed),
            "total_available": str(allocated - reserved - consumed),
            "regions_count": len(regions),
            "parks_count": parks_count,
            "campaigns_count": campaigns_count,
            "budgets_count": budget_qs.count(),
        },
        "regions": [region_data[r["id"]] for r in regions],
        "parks": parks_data,
        "categories": cat_data,
        "subcategories": subcat_data,
        "campaigns": campaign_data,
    }
