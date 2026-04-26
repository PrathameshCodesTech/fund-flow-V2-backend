"""
Horizon FY27 Marketing Budget seed data.

Idempotent — safe to rerun. Creates missing records, updates existing ones.
Does not delete or duplicate anything.

Org structure:
  Horizon
  └── Corporate / North / South / West / Incity  (scope nodes)

Budgets:
  FY27 Marketing - Corporate   → scope: Corporate
  FY27 Marketing - Park Branding → scope: Corporate
  FY27 Marketing - ESG        → scope: Corporate
  FY27 Marketing - BD         → scope: Corporate
  FY27 Marketing - North       → scope: North
  FY27 Marketing - South       → scope: South
  FY27 Marketing - West        → scope: West
  FY27 Marketing - Incity      → scope: Incity

Categories (exact source labels preserved):
  Brand  - Events and Meets
  Brand  - Retainer Fees & Rebranding
  Brand - Content Marketing and Assets
  Brand - Print  and Outdoor
  Others
  Park - Marketing, Branding, Promotions
  ESG Initiatives
  Customer/IPC Events

Subcategories mapped per budget as specified.
"""
from decimal import Decimal

from apps.budgets.models import (
    Budget,
    BudgetCategory,
    BudgetLine,
    BudgetStatus,
    BudgetSubCategory,
    PeriodType,
)
from apps.core.models import NodeType, Organization, ScopeNode


# ---------------------------------------------------------------------------
# Category + subcategory definitions
# ---------------------------------------------------------------------------

_CATEGORY_MAP = {
    "brand-events-meets": {
        "category_name": "Brand  - Events and Meets",
        "category_code": "BRAND-EVENTS-MEETS",
        "subcategories": [
            ("Partners Meet", "BRAND-PARTNERS-MEET"),
            ("Post IPO customer meet", "BRAND-POST-IPO-CUSTOMER-MEET"),
            ("BD - Industry forum membership", "BRAND-BD-INDUSTRY-FORUM-MEMBERSHIP"),
            ("BD - Sponsored industry events & Industry Visits", "BRAND-BD-SPONSORED-INDUSTRY-EVENTS"),
        ],
    },
    "brand-retainer-rebranding": {
        "category_name": "Brand  - Retainer Fees & Rebranding",
        "category_code": "BRAND-RETAINER-REBRANDING",
        "subcategories": [
            ("Branding and creative agency", "BRAND-CREATIVE-AGENCY"),
            ("Digital/social agency", "BRAND-DIGITAL-SOCIAL-AGENCY"),
            ("Website AMC", "BRAND-WEBSITE-AMC"),
            ("Lead Generation", "BRAND-LEAD-GEN"),
            ("Website Hosting Charges", "BRAND-WEBSITE-HOSTING"),
            ("Misc - creative exp", "BRAND-MISC-CREATIVE"),
            ("Press and Media Management", "BRAND-PRESS-MEDIA"),
        ],
    },
    "brand-content-marketing": {
        "category_name": "Brand - Content Marketing and Assets",
        "category_code": "BRAND-CONTENT-MARKETING",
        "subcategories": [
            ("Website Development", "BRAND-WEBSITE-DEV"),
            ("Microblogs Blogs for SEO and SMM", "BRAND-MICROBLOGS-BLOGS"),
            ("Digital Media Buying", "BRAND-DIGITAL-MEDIA-BUYING"),
            ("Brand Manual", "BRAND-MANUAL"),
            ("Corporate Video", "BRAND-CORPORATE-VIDEO"),
            ("Corporate Brochure", "BRAND-CORPORATE-BROCHURE"),
            ("Content Marketing Assets", "BRAND-CONTENT-ASSETS"),
            ("Tenant Newsletter", "BRAND-TENANT-NEWSLETTER"),
            ("Availability Newsletter (for brokers)", "BRAND-AVAILABILITY-NEWSLETTER"),
            ("Promotions for in-city portfolio (NEW)", "BRAND-PROMOTIONS-INCITY"),
            ("Whitepaper/ special publication", "BRAND-WHITEPAPER"),
            ("BD giveaways", "BRAND-BD-GIVEAWAYS"),
        ],
    },
    "brand-print-outdoor": {
        "category_name": "Brand - Print  and Outdoor",
        "category_code": "BRAND-PRINT-OUTDOOR",
        "subcategories": [
            ("Brand level prints collaterals", "BRAND-PRINT-COLLATERALS"),
            ("Trade and Business Print Advts (Business as usual)", "BRAND-PRINT-BUSINESS"),
        ],
    },
    "others": {
        "category_name": "Others",
        "category_code": "OTHERS",
        "subcategories": [
            ("Award Nominations (NEW)", "OTHERS-AWARD-NOMINATIONS"),
            ("Miscellaneous", "OTHERS-MISC"),
            ("L&D cost for marketing team.", "OTHERS-LD-MARKETING"),
            ("Delegate cost leasing & BD team for trade events.", "OTHERS-DELEGATE-LEASING-BD"),
        ],
    },
    "park-marketing": {
        "category_name": "Park - Marketing, Branding, Promotions",
        "category_code": "PARK-MARKETING",
        "subcategories": [
            ("FMO & Park Branding", "PARK-FMO-BRANDING"),
            ("Tenant Engagement", "PARK-TENANT-ENGAGEMENT"),
            ("outdoor medians (local)", "PARK-OUTDOOR-MEDIANS"),
            ("outdoor (local)", "PARK-OUTDOOR-LOCAL"),
            ("Onsite Hoarding perimeter branding", "PARK-ONSITE-HOARDING"),
            ("Upgrades Photoshoot/ Video", "PARK-UPGRADES-PHOTOSHOOT"),
            ("CMVs, Block, Panorama & Timelapse", "PARK-CMV-BLOCK-PANO"),
            ("CMVs, Panorama & Timelapse", "PARK-CMV-PANO-TIMELAPSE"),
            ("Before After shoot", "PARK-BEFORE-AFTER-SHOOT"),
            ("Before/after", "PARK-BEFORE-AFTER"),
            ("Park Marketing videos", "PARK-MARKETING-VIDEOS"),
            ("Client Visits Branding", "PARK-CLIENT-VISITS-BRANDING"),
            ("Client Visits", "PARK-CLIENT-VISITS"),
            ("Ground breaking event", "PARK-GROUND-BREAKING-EVENT"),
            ("Ground Breaking Event", "PARK-GROUND-BREAKING-EVENT-2"),
            ("Brochure Printing", "PARK-BROCHURE-PRINTING"),
            ("Misc.", "PARK-MISC"),
        ],
    },
    "esg-initiatives": {
        "category_name": "ESG Initiatives",
        "category_code": "ESG-INITIATIVES",
        "subcategories": [
            (
                "Master Class Event - Sustainable IRE (Proposed theme for post IPO customer meet)",
                "ESG-MASTER-CLASS",
            ),
            ("Skill Center Inauguration at Farukhnagar II", "ESG-SKILL-CENTER"),
            (
                "National Safety Week (March), National Public Health Week (April) - Tenant Engagement",
                "ESG-NATIONAL-SAFETY-WEEK",
            ),
            ("Video/Photo documentation", "ESG-VIDEO-PHOTO-DOC"),
        ],
    },
    "customer-ipc-events": {
        "category_name": "Customer/IPC Events",
        "category_code": "CUSTOMER-IPC-EVENTS",
        "subcategories": [
            ("BD Memberships", "IPC-BD-MEMBERSHIPS"),
            ("Trade Events", "IPC-TRADE-EVENTS"),
        ],
    },
}

# Extra Park Branding subcategories to add if not already present
_PARK_BRANDING_EXTRAS = [
    ("park signage", "PARK-SIGNAGE"),
    ("building signage", "PARK-BUILDING-SIGNAGE"),
    ("minor signages", "PARK-MINOR-SIGNAGES"),
    ("Perimeter branding", "PARK-PERIMETER-BRANDING"),
    ("digital hoarding", "PARK-DIGITAL-HOARDING"),
    ("block signage", "PARK-BLOCK-SIGNAGE"),
    ("full park signages", "PARK-FULL-PARK-SIGNAGES"),
]

_PARK_BRANDING_EXTRA_CODES = [code for _, code in _PARK_BRANDING_EXTRAS]

# ---------------------------------------------------------------------------
# Budget definitions
# ---------------------------------------------------------------------------

_PARK_MARKETING_SUBCODES = [
    # North
    "PARK-FMO-BRANDING",
    "PARK-TENANT-ENGAGEMENT",
    "PARK-OUTDOOR-MEDIANS",
    "PARK-ONSITE-HOARDING",
    "PARK-UPGRADES-PHOTOSHOOT",
    "PARK-CMV-BLOCK-PANO",
    "PARK-BEFORE-AFTER-SHOOT",
    "PARK-MARKETING-VIDEOS",
    "PARK-CLIENT-VISITS-BRANDING",
    "PARK-GROUND-BREAKING-EVENT",
    "PARK-MISC",
    # South (adds PARK-OUTDOOR-LOCAL, PARK-CMV-PANO-TIMELAPSE, PARK-BEFORE-AFTER, PARK-CLIENT-VISITS, PARK-GROUND-BREAKING-EVENT-2)
    "PARK-OUTDOOR-LOCAL",
    "PARK-CMV-PANO-TIMELAPSE",
    "PARK-BEFORE-AFTER",
    "PARK-CLIENT-VISITS",
    "PARK-GROUND-BREAKING-EVENT-2",
]

# Exact approved FY27 amounts derived from the workbook.
#
# Important modeling choice:
# - Budget headers/lines below mirror the Excel workbook totals as closely as the
#   current schema allows.
# - This schema only stores FY27 allocated amounts. FY26 budget, spent, balance,
#   and % change are not modeled as first-class fields yet.
# - The workbook does NOT define "FY27 Marketing - BD" and "FY27 Marketing - Park Branding"
#   as separate monetary pools. They remain in the system as placeholder budgets so the
#   chosen budget structure still exists, but their allocated amounts stay zero until the
#   business decides to break those out intentionally from the workbook totals.
_BUDGET_LINE_AMOUNTS = {
    "FY27-MKT-CORP": {
        "BRAND-PARTNERS-MEET": Decimal("7000000"),
        "BRAND-POST-IPO-CUSTOMER-MEET": Decimal("2000000"),
        "BRAND-BD-INDUSTRY-FORUM-MEMBERSHIP": Decimal("400000"),
        "BRAND-BD-SPONSORED-INDUSTRY-EVENTS": Decimal("7400000"),
        "BRAND-CREATIVE-AGENCY": Decimal("2088000"),
        "BRAND-DIGITAL-SOCIAL-AGENCY": Decimal("1650000"),
        "BRAND-WEBSITE-AMC": Decimal("598000"),
        "BRAND-LEAD-GEN": Decimal("0"),
        "BRAND-WEBSITE-HOSTING": Decimal("410000"),
        "BRAND-MISC-CREATIVE": Decimal("360000"),
        "BRAND-PRESS-MEDIA": Decimal("4271800"),
        "BRAND-WEBSITE-DEV": Decimal("450000"),
        "BRAND-MICROBLOGS-BLOGS": Decimal("100000"),
        "BRAND-DIGITAL-MEDIA-BUYING": Decimal("2940000"),
        "BRAND-MANUAL": Decimal("0"),
        "BRAND-CORPORATE-VIDEO": Decimal("900000"),
        "BRAND-CORPORATE-BROCHURE": Decimal("800000"),
        "BRAND-CONTENT-ASSETS": Decimal("5880000"),
        "BRAND-TENANT-NEWSLETTER": Decimal("250000"),
        "BRAND-AVAILABILITY-NEWSLETTER": Decimal("200000"),
        "BRAND-PROMOTIONS-INCITY": Decimal("0"),
        "BRAND-WHITEPAPER": Decimal("1000000"),
        "BRAND-BD-GIVEAWAYS": Decimal("1250000"),
        "BRAND-PRINT-COLLATERALS": Decimal("1000000"),
        "BRAND-PRINT-BUSINESS": Decimal("1275000"),
        "OTHERS-AWARD-NOMINATIONS": Decimal("312500"),
        "OTHERS-MISC": Decimal("1000000"),
        "OTHERS-LD-MARKETING": Decimal("245000"),
        "OTHERS-DELEGATE-LEASING-BD": Decimal("500000"),
    },
    "FY27-MKT-NORTH": {
        "PARK-FMO-BRANDING": Decimal("8670000"),
        "PARK-TENANT-ENGAGEMENT": Decimal("4400000"),
        "PARK-OUTDOOR-MEDIANS": Decimal("2000000"),
        "PARK-ONSITE-HOARDING": Decimal("590000"),
        "PARK-UPGRADES-PHOTOSHOOT": Decimal("800000"),
        "PARK-CMV-BLOCK-PANO": Decimal("225000"),
        "PARK-BEFORE-AFTER-SHOOT": Decimal("50000"),
        "PARK-MARKETING-VIDEOS": Decimal("50000"),
        "PARK-CLIENT-VISITS-BRANDING": Decimal("200000"),
        "PARK-GROUND-BREAKING-EVENT": Decimal("0"),
        "PARK-MISC": Decimal("250000"),
    },
    "FY27-MKT-SOUTH": {
        "PARK-FMO-BRANDING": Decimal("19800000"),
        "PARK-TENANT-ENGAGEMENT": Decimal("6600000"),
        "PARK-OUTDOOR-LOCAL": Decimal("2000000"),
        "PARK-ONSITE-HOARDING": Decimal("2907000"),
        "PARK-UPGRADES-PHOTOSHOOT": Decimal("800000"),
        "PARK-CMV-PANO-TIMELAPSE": Decimal("1062500"),
        "PARK-BEFORE-AFTER": Decimal("150000"),
        "PARK-MARKETING-VIDEOS": Decimal("850000"),
        "PARK-CLIENT-VISITS": Decimal("450000"),
        "PARK-GROUND-BREAKING-EVENT-2": Decimal("0"),
        "PARK-MISC": Decimal("700000"),
    },
    "FY27-MKT-WEST": {
        "PARK-FMO-BRANDING": Decimal("11850000"),
        "PARK-TENANT-ENGAGEMENT": Decimal("2200000"),
        "PARK-OUTDOOR-LOCAL": Decimal("4000000"),
        "PARK-UPGRADES-PHOTOSHOOT": Decimal("400000"),
        "PARK-ONSITE-HOARDING": Decimal("9500000"),
        "PARK-CMV-PANO-TIMELAPSE": Decimal("700000"),
        "PARK-MARKETING-VIDEOS": Decimal("350000"),
        "PARK-CLIENT-VISITS": Decimal("150000"),
        "PARK-GROUND-BREAKING-EVENT-2": Decimal("1500000"),
        "PARK-MISC": Decimal("400000"),
    },
    "FY27-MKT-INCITY": {
        "PARK-FMO-BRANDING": Decimal("6000000"),
        "PARK-BROCHURE-PRINTING": Decimal("425000"),
        "PARK-OUTDOOR-LOCAL": Decimal("2400000"),
        "PARK-ONSITE-HOARDING": Decimal("320000"),
        "PARK-CMV-PANO-TIMELAPSE": Decimal("1800000"),
        "PARK-MARKETING-VIDEOS": Decimal("0"),
        "PARK-CLIENT-VISITS": Decimal("850000"),
        "PARK-GROUND-BREAKING-EVENT-2": Decimal("0"),
        "PARK-MISC": Decimal("850000"),
    },
    "FY27-MKT-PARK": {
        "PARK-FMO-BRANDING": Decimal("0"),
    },
    "FY27-MKT-ESG": {
        "ESG-MASTER-CLASS": Decimal("0"),
        "ESG-SKILL-CENTER": Decimal("1000000"),
        "ESG-NATIONAL-SAFETY-WEEK": Decimal("1300000"),
        "ESG-VIDEO-PHOTO-DOC": Decimal("2000000"),
    },
    "FY27-MKT-BD": {
        "IPC-BD-MEMBERSHIPS": Decimal("0"),
        "IPC-TRADE-EVENTS": Decimal("0"),
    },
}

_BUDGET_DEFS = {
    "FY27-MKT-CORP": {
        "name": "FY27 Marketing - Corporate",
        "scope_code": "corporate",
        # All subcategories from brand-events-meets, brand-retainer-rebranding,
        # brand-content-marketing, brand-print-outdoor, others
        "categories": ["brand-events-meets", "brand-retainer-rebranding", "brand-content-marketing", "brand-print-outdoor", "others"],
        "allocated_amount": Decimal("0"),
    },
    "FY27-MKT-NORTH": {
        "name": "FY27 Marketing - North",
        "scope_code": "north",
        "categories": ["park-marketing"],
        "park_subcodes": [
            "PARK-FMO-BRANDING", "PARK-TENANT-ENGAGEMENT", "PARK-OUTDOOR-MEDIANS",
            "PARK-ONSITE-HOARDING", "PARK-UPGRADES-PHOTOSHOOT", "PARK-CMV-BLOCK-PANO",
            "PARK-BEFORE-AFTER-SHOOT", "PARK-MARKETING-VIDEOS", "PARK-CLIENT-VISITS-BRANDING",
            "PARK-GROUND-BREAKING-EVENT", "PARK-MISC",
        ],
        "allocated_amount": Decimal("0"),
    },
    "FY27-MKT-SOUTH": {
        "name": "FY27 Marketing - South",
        "scope_code": "south",
        "categories": ["park-marketing"],
        "park_subcodes": [
            "PARK-FMO-BRANDING", "PARK-TENANT-ENGAGEMENT", "PARK-OUTDOOR-LOCAL",
            "PARK-ONSITE-HOARDING", "PARK-UPGRADES-PHOTOSHOOT", "PARK-CMV-PANO-TIMELAPSE",
            "PARK-BEFORE-AFTER", "PARK-MARKETING-VIDEOS", "PARK-CLIENT-VISITS",
            "PARK-GROUND-BREAKING-EVENT-2", "PARK-MISC",
        ],
        "allocated_amount": Decimal("0"),
    },
    "FY27-MKT-WEST": {
        "name": "FY27 Marketing - West",
        "scope_code": "west",
        "categories": ["park-marketing"],
        "park_subcodes": [
            "PARK-FMO-BRANDING", "PARK-TENANT-ENGAGEMENT", "PARK-OUTDOOR-LOCAL",
            "PARK-UPGRADES-PHOTOSHOOT", "PARK-ONSITE-HOARDING", "PARK-CMV-PANO-TIMELAPSE",
            "PARK-MARKETING-VIDEOS", "PARK-CLIENT-VISITS", "PARK-GROUND-BREAKING-EVENT-2",
            "PARK-MISC",
        ],
        "allocated_amount": Decimal("0"),
    },
    "FY27-MKT-INCITY": {
        "name": "FY27 Marketing - Incity",
        "scope_code": "incity",
        "categories": ["park-marketing"],
        "park_subcodes": [
            "PARK-FMO-BRANDING", "PARK-BROCHURE-PRINTING", "PARK-OUTDOOR-LOCAL",
            "PARK-ONSITE-HOARDING", "PARK-CMV-PANO-TIMELAPSE", "PARK-MARKETING-VIDEOS",
            "PARK-CLIENT-VISITS", "PARK-GROUND-BREAKING-EVENT-2", "PARK-MISC",
        ],
        "allocated_amount": Decimal("0"),
    },
    "FY27-MKT-PARK": {
        "name": "FY27 Marketing - Park Branding",
        "scope_code": "corporate",
        "categories": ["park-marketing"],
        # Uses all base park-marketing subcodes + extras
        "park_subcodes": _PARK_MARKETING_SUBCODES + _PARK_BRANDING_EXTRA_CODES,
        "allocated_amount": Decimal("0"),
    },
    "FY27-MKT-ESG": {
        "name": "FY27 Marketing - ESG",
        "scope_code": "corporate",
        "categories": ["esg-initiatives"],
        "allocated_amount": Decimal("0"),
    },
    "FY27-MKT-BD": {
        "name": "FY27 Marketing - BD",
        "scope_code": "corporate",
        "categories": ["customer-ipc-events"],
        "allocated_amount": Decimal("0"),
    },
}

# Scope node definitions: code -> name
_SCOPE_DEFS = [
    ("corporate", "Corporate"),
    ("north", "North"),
    ("south", "South"),
    ("west", "West"),
    ("incity", "Incity"),
]


def seed_horizon_marketing_budget():
    """
    Main idempotent seed function.
    Returns a summary dict with counts of records created/updated.
    """
    counters = {
        "org_created": False,
        "scope_nodes_created": 0,
        "categories_created": 0,
        "subcategories_created": 0,
        "budgets_created": 0,
        "budgets_updated": 0,
        "budget_lines_created": 0,
        "park_branding_extras_added": 0,
    }

    # 1. Organization
    org, org_created = Organization.objects.get_or_create(
        code="horizon",
        defaults={"name": "Horizon", "is_active": True},
    )
    counters["org_created"] = org_created

    # 2. Scope nodes
    scope_map = {}
    for code, name in _SCOPE_DEFS:
        node, node_created = ScopeNode.objects.get_or_create(
            org=org,
            code=code,
            defaults={
                "name": name,
                "node_type": NodeType.REGION,
                "parent": None,
                "path": f"/{org.code}/{code}",
                "depth": 0,
                "is_active": True,
            },
        )
        scope_map[code] = node
        if node_created:
            counters["scope_nodes_created"] += 1

    # 3. Categories + subcategories
    cat_map = {}
    for key, spec in _CATEGORY_MAP.items():
        cat, cat_created = BudgetCategory.objects.get_or_create(
            org=org,
            code=spec["category_code"],
            defaults={"name": spec["category_name"], "is_active": True},
        )
        cat_map[key] = cat
        if cat_created:
            counters["categories_created"] += 1

        for sub_name, sub_code in spec["subcategories"]:
            sub, sub_created = BudgetSubCategory.objects.get_or_create(
                category=cat,
                code=sub_code,
                defaults={"name": sub_name, "is_active": True},
            )
            if sub_created:
                counters["subcategories_created"] += 1

    # 4. Budgets + BudgetLines
    for budget_code, spec in _BUDGET_DEFS.items():
        scope_node = scope_map[spec["scope_code"]]
        line_amounts = _BUDGET_LINE_AMOUNTS.get(budget_code, {})
        budget_allocated_amount = sum(line_amounts.values(), Decimal("0"))
        budget_defaults = {
            "name": spec["name"],
            "scope_node": scope_node,
            "financial_year": "2026-27",
            "period_type": PeriodType.YEARLY,
            "currency": "INR",
            "status": BudgetStatus.ACTIVE,
            "allocated_amount": budget_allocated_amount,
            "reserved_amount": Decimal("0"),
            "consumed_amount": Decimal("0"),
        }

        budget, budget_created = Budget.objects.get_or_create(
            org=org,
            scope_node=scope_node,
            financial_year="2026-27",
            code=budget_code,
            defaults=budget_defaults,
        )
        if budget_created:
            counters["budgets_created"] += 1
        else:
            # Idempotent update: refresh header fields and workbook-backed total.
            changed = False
            for f in ("name", "status", "currency", "period_type", "allocated_amount"):
                if getattr(budget, f) != budget_defaults[f]:
                    setattr(budget, f, budget_defaults[f])
                    changed = True
            if changed:
                budget.save(update_fields=["name", "status", "currency", "period_type", "allocated_amount", "updated_at"])
            counters["budgets_updated"] += 1

        # For Park Branding budget, add extra subcategories
        if budget_code == "FY27-MKT-PARK":
            park_cat = cat_map["park-marketing"]
            for sub_name, sub_code in _PARK_BRANDING_EXTRAS:
                sub, sub_created = BudgetSubCategory.objects.get_or_create(
                    category=park_cat,
                    code=sub_code,
                    defaults={"name": sub_name, "is_active": True},
                )
                if sub_created:
                    counters["subcategories_created"] += 1
                    counters["park_branding_extras_added"] += 1

        # BudgetLines for each category in this budget
        for cat_key in spec["categories"]:
            cat = cat_map[cat_key]
            # For park-marketing budgets, use specific subcode lists (per region spec)
            # For other budgets, use all subcategories for that category
            if cat_key == "park-marketing" and "park_subcodes" in spec:
                target_subs = BudgetSubCategory.objects.filter(
                    category=cat,
                    code__in=spec["park_subcodes"],
                    is_active=True,
                )
            else:
                target_subs = BudgetSubCategory.objects.filter(category=cat, is_active=True)
            for sub in target_subs:
                target_amount = line_amounts.get(sub.code, Decimal("0"))
                line, line_created = BudgetLine.objects.get_or_create(
                    budget=budget,
                    category=cat,
                    subcategory=sub,
                    defaults={"allocated_amount": target_amount},
                )
                if line_created:
                    counters["budget_lines_created"] += 1
                elif line.allocated_amount != target_amount:
                    line.allocated_amount = target_amount
                    line.save(update_fields=["allocated_amount", "updated_at"])

    return counters


def print_seed_summary(counters: dict, stdout=None):
    """Print a human-readable summary."""
    if stdout is None:
        import sys
        stdout = sys.stdout

    w = stdout.write

    w("")
    w("=" * 62)
    w("  Horizon FY27 Marketing Budget Seed Complete")
    w("=" * 62)
    w("")
    w(f"  Organization 'Horizon'       : {'created' if counters.get('org_created') else 'found (already exists)'}")
    w(f"  Scope nodes (5)              : {counters.get('scope_nodes_created', 0)} created, all found")
    w(f"  Categories (8)               : {counters.get('categories_created', 0)} created, all found")
    w(f"  Subcategories                : {counters.get('subcategories_created', 0)} created/updated")
    w(f"  Budgets (8 headers)          : {counters.get('budgets_created', 0)} created, {counters.get('budgets_updated', 0)} updated")
    w(f"  BudgetLines                  : {counters.get('budget_lines_created', 0)} created")
    w(f"  Park Branding extras added   : {counters.get('park_branding_extras_added', 0)}")
    w("")
    w("  Budgets:")
    for code, spec in _BUDGET_DEFS.items():
        w(f"    {code:<20} -> {spec['name']}")
    w("")
    w("  Idempotent: safe to rerun — no duplicate categories,")
    w("  subcategories, or budgets will be created.")
    w("=" * 62)
