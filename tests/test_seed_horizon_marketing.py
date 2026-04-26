"""
Tests for seed_horizon_marketing_budget.

Tests:
1. Creates Horizon org structure if missing
2. Creates the 5 scope nodes
3. Creates exact categories and subcategories
4. Creates the 8 budgets in the correct scopes
5. Creates budget lines under the right budgets
6. Is idempotent on rerun
"""
import pytest
from decimal import Decimal

from apps.budgets.models import (
    Budget,
    BudgetCategory,
    BudgetLine,
    BudgetStatus,
    BudgetSubCategory,
    PeriodType,
)
from apps.budgets.seed_horizon import (
    _BUDGET_DEFS,
    _BUDGET_LINE_AMOUNTS,
    _CATEGORY_MAP,
    _PARK_BRANDING_EXTRAS,
    _SCOPE_DEFS,
    seed_horizon_marketing_budget,
)
from apps.core.models import NodeType, Organization, ScopeNode


@pytest.fixture
def horizon_org(db):
    """Clean slate: remove any existing Horizon data before each test."""
    org = Organization.objects.filter(code="horizon").first()
    if org:
        # Delete in correct order to avoid FK violations
        BudgetLine.objects.filter(budget__org=org).delete()
        Budget.objects.filter(org=org).delete()
        BudgetCategory.objects.filter(org=org).delete()
        BudgetSubCategory.objects.filter(category__org=org).delete()
        ScopeNode.objects.filter(org=org).delete()
        org.delete()
    return None


class TestHorizonOrgCreation:
    def test_creates_horizon_org_if_missing(self, db, horizon_org):
        """Org is created with correct code when missing."""
        org_count_before = Organization.objects.filter(code="horizon").count()
        seed_horizon_marketing_budget()
        org_count_after = Organization.objects.count()
        assert org_count_after == org_count_before + 1
        org = Organization.objects.get(code="horizon")
        assert org.name == "Horizon"
        assert org.is_active is True

    def test_horizon_org_idempotent_on_rerun(self, db, horizon_org):
        """Re-running seed does not create duplicate org."""
        seed_horizon_marketing_budget()
        seed_horizon_marketing_budget()
        assert Organization.objects.filter(code="horizon").count() == 1


class TestScopeNodeCreation:
    def test_creates_five_scope_nodes(self, db, horizon_org):
        """All five scope nodes (Corporate/North/South/West/Incity) are created."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        nodes = ScopeNode.objects.filter(org=org)
        assert nodes.count() == 5
        codes = set(nodes.values_list("code", flat=True))
        expected_codes = {"corporate", "north", "south", "west", "incity"}
        assert codes == expected_codes

    def test_scope_nodes_have_correct_node_type(self, db, horizon_org):
        """Scope nodes are created with REGION node type."""
        seed_horizon_marketing_budget()
        for code, _ in _SCOPE_DEFS:
            node = ScopeNode.objects.get(org__code="horizon", code=code)
            assert node.node_type == NodeType.REGION
            assert node.is_active is True

    def test_scope_nodes_idempotent(self, db, horizon_org):
        """Re-running seed does not duplicate scope nodes."""
        seed_horizon_marketing_budget()
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        assert ScopeNode.objects.filter(org=org).count() == 5


class TestCategoryAndSubcategoryCreation:
    def test_creates_eight_categories(self, db, horizon_org):
        """All 8 categories are created with exact source label names."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        cats = BudgetCategory.objects.filter(org=org)
        assert cats.count() == 8

    def test_categories_have_exact_source_labels(self, db, horizon_org):
        """Category names match the exact agreed source labels."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        cat_map = {
            c.code: c.name
            for c in BudgetCategory.objects.filter(org=org)
        }
        for key, spec in _CATEGORY_MAP.items():
            assert cat_map[spec["category_code"]] == spec["category_name"], (
                f"Category {spec['category_code']}: expected '{spec['category_name']}', "
                f"got '{cat_map.get(spec['category_code'])}'"
            )

    def test_creates_all_subcategories(self, db, horizon_org):
        """Subcategories are created for each category (52 base + 7 Park Branding extras = 59)."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        base_count = sum(len(spec["subcategories"]) for spec in _CATEGORY_MAP.values())
        extras_count = len(_PARK_BRANDING_EXTRAS)
        expected_count = base_count + extras_count
        actual_count = BudgetSubCategory.objects.filter(category__org=org).count()
        assert actual_count == expected_count, (
            f"Expected {expected_count} (base={base_count} + extras={extras_count}), got {actual_count}"
        )

    def test_subcategory_names_preserve_source_fidelity(self, db, horizon_org):
        """Subcategory names match exact source labels including inconsistencies."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")

        # Spot-check a few specific names
        checks = [
            ("PARK-BEFORE-AFTER", "Before/after"),
            ("PARK-GROUND-BREAKING-EVENT-2", "Ground Breaking Event"),
            ("BRAND-PROMOTIONS-INCITY", "Promotions for in-city portfolio (NEW)"),
            ("OTHERS-DELEGATE-LEASING-BD", "Delegate cost leasing & BD team for trade events."),
            ("ESG-NATIONAL-SAFETY-WEEK",
             "National Safety Week (March), National Public Health Week (April) - Tenant Engagement"),
        ]
        for code, expected_name in checks:
            sub = BudgetSubCategory.objects.get(code=code)
            assert sub.name == expected_name


class TestBudgetCreation:
    def test_creates_eight_budgets(self, db, horizon_org):
        """All 8 budget headers are created."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        assert Budget.objects.filter(org=org).count() == 8

    def test_budgets_have_correct_codes(self, db, horizon_org):
        """Budget headers use the correct stable codes."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        codes = set(Budget.objects.filter(org=org).values_list("code", flat=True))
        expected_codes = set(_BUDGET_DEFS.keys())
        assert codes == expected_codes

    def test_budgets_have_correct_default_values(self, db, horizon_org):
        """Budgets have correct core defaults and allocated totals derived from workbook-backed lines."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        for budget in Budget.objects.filter(org=org):
            assert budget.financial_year == "2026-27"
            assert budget.period_type == PeriodType.YEARLY
            assert budget.currency == "INR"
            assert budget.status == BudgetStatus.ACTIVE
            expected_total = sum(_BUDGET_LINE_AMOUNTS[budget.code].values(), Decimal("0"))
            assert budget.allocated_amount == expected_total
            assert budget.reserved_amount == Decimal("0")
            assert budget.consumed_amount == Decimal("0")

    def test_budgets_attached_to_correct_scopes(self, db, horizon_org):
        """Budgets are attached to their specified scope nodes."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        scope_map = {n.code: n for n in ScopeNode.objects.filter(org=org)}
        for budget_code, spec in _BUDGET_DEFS.items():
            budget = Budget.objects.get(org=org, code=budget_code)
            expected_scope = scope_map[spec["scope_code"]]
            assert budget.scope_node == expected_scope

    def test_budget_names_match_spec(self, db, horizon_org):
        """Budget names match the spec exactly."""
        seed_horizon_marketing_budget()
        for budget_code, spec in _BUDGET_DEFS.items():
            budget = Budget.objects.get(code=budget_code)
            assert budget.name == spec["name"]


class TestBudgetLineCreation:
    def test_creates_budget_lines_under_corporate_budget(self, db, horizon_org):
        """FY27-MKT-CORP budget gets lines for Brand categories + Others."""
        seed_horizon_marketing_budget()
        corp_budget = Budget.objects.get(code="FY27-MKT-CORP")
        lines = BudgetLine.objects.filter(budget=corp_budget)
        # Corporate budget covers 5 categories: brand-events-meets,
        # brand-retainer-rebranding, brand-content-marketing, brand-print-outdoor, others
        cat_codes = set(lines.values_list("category__code", flat=True))
        assert "BRAND-EVENTS-MEETS" in cat_codes
        assert "BRAND-RETAINER-REBRANDING" in cat_codes
        assert "BRAND-CONTENT-MARKETING" in cat_codes
        assert "BRAND-PRINT-OUTDOOR" in cat_codes
        assert "OTHERS" in cat_codes

    def test_creates_budget_lines_under_north_budget(self, db, horizon_org):
        """FY27-MKT-NORTH budget gets Park - Marketing subcategories."""
        seed_horizon_marketing_budget()
        north_budget = Budget.objects.get(code="FY27-MKT-NORTH")
        lines = BudgetLine.objects.filter(budget=north_budget)
        sub_codes = set(lines.values_list("subcategory__code", flat=True))
        assert "PARK-FMO-BRANDING" in sub_codes
        assert "PARK-TENANT-ENGAGEMENT" in sub_codes
        assert "PARK-OUTDOOR-MEDIANS" in sub_codes

    def test_creates_budget_lines_under_south_budget(self, db, horizon_org):
        """FY27-MKT-SOUTH budget gets correct Park subcategories."""
        seed_horizon_marketing_budget()
        south_budget = Budget.objects.get(code="FY27-MKT-SOUTH")
        lines = BudgetLine.objects.filter(budget=south_budget)
        sub_codes = set(lines.values_list("subcategory__code", flat=True))
        assert "PARK-FMO-BRANDING" in sub_codes
        assert "PARK-OUTDOOR-LOCAL" in sub_codes
        assert "PARK-ONSITE-HOARDING" in sub_codes

    def test_creates_budget_lines_under_west_budget(self, db, horizon_org):
        """FY27-MKT-WEST budget gets Park subcategories."""
        seed_horizon_marketing_budget()
        west_budget = Budget.objects.get(code="FY27-MKT-WEST")
        lines = BudgetLine.objects.filter(budget=west_budget)
        sub_codes = set(lines.values_list("subcategory__code", flat=True))
        assert "PARK-FMO-BRANDING" in sub_codes
        assert "PARK-OUTDOOR-LOCAL" in sub_codes

    def test_creates_budget_lines_under_incity_budget(self, db, horizon_org):
        """FY27-MKT-INCITY budget includes Brochure Printing."""
        seed_horizon_marketing_budget()
        incity_budget = Budget.objects.get(code="FY27-MKT-INCITY")
        lines = BudgetLine.objects.filter(budget=incity_budget)
        sub_codes = set(lines.values_list("subcategory__code", flat=True))
        assert "PARK-FMO-BRANDING" in sub_codes
        assert "PARK-BROCHURE-PRINTING" in sub_codes

    def test_creates_budget_lines_under_esg_budget(self, db, horizon_org):
        """FY27-MKT-ESG budget gets ESG subcategories."""
        seed_horizon_marketing_budget()
        esg_budget = Budget.objects.get(code="FY27-MKT-ESG")
        lines = BudgetLine.objects.filter(budget=esg_budget)
        sub_codes = set(lines.values_list("subcategory__code", flat=True))
        assert "ESG-MASTER-CLASS" in sub_codes
        assert "ESG-SKILL-CENTER" in sub_codes
        assert "ESG-NATIONAL-SAFETY-WEEK" in sub_codes

    def test_creates_budget_lines_under_bd_budget(self, db, horizon_org):
        """FY27-MKT-BD budget gets Customer/IPC subcategories."""
        seed_horizon_marketing_budget()
        bd_budget = Budget.objects.get(code="FY27-MKT-BD")
        lines = BudgetLine.objects.filter(budget=bd_budget)
        sub_codes = set(lines.values_list("subcategory__code", flat=True))
        assert "IPC-BD-MEMBERSHIPS" in sub_codes
        assert "IPC-TRADE-EVENTS" in sub_codes


class TestParkBrandingExtras:
    def test_park_branding_budget_gets_extra_subcategories(self, db, horizon_org):
        """Park Branding budget adds the extra 7 subcategories."""
        seed_horizon_marketing_budget()
        park_budget = Budget.objects.get(code="FY27-MKT-PARK")
        extras_codes = {code for _, code in _PARK_BRANDING_EXTRAS}
        existing_codes = set(
            BudgetLine.objects.filter(budget=park_budget)
            .values_list("subcategory__code", flat=True)
        )
        assert extras_codes.issubset(existing_codes), (
            f"Missing extras: {extras_codes - existing_codes}"
        )

    def test_park_branding_extras_only_added_once(self, db, horizon_org):
        """Re-running seed does not duplicate the extra Park Branding subcategories."""
        seed_horizon_marketing_budget()
        seed_horizon_marketing_budget()
        park_cat = BudgetCategory.objects.get(code="PARK-MARKETING")
        extras_codes = {code for _, code in _PARK_BRANDING_EXTRAS}
        for code in extras_codes:
            count = BudgetSubCategory.objects.filter(category=park_cat, code=code).count()
            assert count == 1, f"{code} has {count} rows (expected 1)"


class TestIdempotency:
    def test_seed_idempotent_no_new_records_on_second_run(self, db, horizon_org):
        """Re-running seed creates no new records (all get_or_create)."""
        seed_horizon_marketing_budget()

        org_before = Organization.objects.get(code="horizon")
        node_count_before = ScopeNode.objects.filter(org=org_before).count()
        cat_count_before = BudgetCategory.objects.filter(org=org_before).count()
        sub_count_before = BudgetSubCategory.objects.filter(category__org=org_before).count()
        budget_count_before = Budget.objects.filter(org=org_before).count()
        line_count_before = BudgetLine.objects.filter(budget__org=org_before).count()

        seed_horizon_marketing_budget()

        org_after = Organization.objects.get(code="horizon")
        assert ScopeNode.objects.filter(org=org_after).count() == node_count_before
        assert BudgetCategory.objects.filter(org=org_after).count() == cat_count_before
        assert BudgetSubCategory.objects.filter(category__org=org_after).count() == sub_count_before
        assert Budget.objects.filter(org=org_after).count() == budget_count_before
        assert BudgetLine.objects.filter(budget__org=org_after).count() == line_count_before

    def test_budget_header_allocated_amount_sum_matches_lines(self, db, horizon_org):
        """After seeding, budget header allocated_amount equals sum of line allocated_amounts."""
        seed_horizon_marketing_budget()
        org = Organization.objects.get(code="horizon")
        for budget in Budget.objects.filter(org=org):
            line_sum = sum(
                bl.allocated_amount for bl in BudgetLine.objects.filter(budget=budget)
            )
            assert budget.allocated_amount == line_sum, (
                f"Budget {budget.code}: header allocated={budget.allocated_amount}, "
                f"line sum={line_sum}"
            )


# ---------------------------------------------------------------------------
# Tests for seed_horizon_me_workflow normalization safety
# ---------------------------------------------------------------------------

import pytest
from django.core.management import call_command
from io import StringIO

from apps.access.models import Role, UserRoleAssignment
from apps.core.models import NodeType, Organization, ScopeNode
from apps.users.models import User


@pytest.fixture
def horizon_me_setup(db, horizon_org):
    """Seed the base Horizon org, then add the marketing node structure."""
    from apps.budgets.seed_horizon import seed_horizon_marketing_budget
    seed_horizon_marketing_budget()
    org = Organization.objects.get(code="horizon")
    marketing = ScopeNode.objects.create(
        org=org, parent=None, name="Marketing", code="marketing",
        node_type=NodeType.DEPARTMENT, path="/horizon/marketing", depth=0,
    )
    for code, name in [("north", "North"), ("south", "South"), ("west", "West"), ("incity", "Incity")]:
        if not ScopeNode.objects.filter(org=org, code=code).exists():
            ScopeNode.objects.create(
                org=org, parent=marketing, name=name, code=code,
                node_type=NodeType.REGION, path=f"/horizon/marketing/{code}", depth=1,
            )
    return org, marketing


@pytest.fixture
def unrelated_me_user(db, horizon_me_setup):
    """A legitimate marketing_executive assigned at a park node (not the 4 seeded users)."""
    org, _ = horizon_me_setup
    me_role = Role.objects.get_or_create(
        org=org, code="marketing_executive", defaults={"name": "Marketing Executive", "is_active": True},
    )[0]
    north = ScopeNode.objects.get(org=org, code="north")
    user = User.objects.create_user(email="other_me@horizon.local", password="pass")
    assignment = UserRoleAssignment.objects.create(user=user, role=me_role, scope_node=north)
    return assignment


class TestMeWorkflowSeedNormalization:
    def test_unrelated_me_assignment_at_park_node_is_preserved(
        self, db, horizon_me_setup, unrelated_me_user,
    ):
        """
        The normalization only removes assignments for the 4 known-bad seeded users.
        An unrelated marketing_executive assignment at a park node must not be deleted.
        """
        org, _ = horizon_me_setup
        me_role = Role.objects.get_or_create(
            org=org, code="marketing_executive", defaults={"name": "Marketing Executive", "is_active": True},
        )[0]
        north = ScopeNode.objects.get(org=org, code="north")

        # Verify the unrelated assignment exists before seed
        assert UserRoleAssignment.objects.filter(user=unrelated_me_user.user, role=me_role, scope_node=north).exists()

        # Run the seed
        out = StringIO()
        call_command("seed_horizon_me_workflow", stdout=out)

        # Unrelated assignment must still exist after seed
        assert UserRoleAssignment.objects.filter(
            user=unrelated_me_user.user, role=me_role, scope_node=north,
        ).exists(), "Unrelated ME assignment at park node was incorrectly deleted"

    def test_only_seeded_bad_user_assignments_are_deleted(
        self, db, horizon_me_setup,
    ):
        """
        Only the 4 known-bad seeded-user assignments at park nodes are removed.
        All other marketing_executive assignments are untouched.
        """
        org, _ = horizon_me_setup
        me_role = Role.objects.get_or_create(
            org=org, code="marketing_executive", defaults={"name": "Marketing Executive", "is_active": True},
        )[0]
        north = ScopeNode.objects.get(org=org, code="north")

        # Create a "bad" assignment for one of the known-bad emails at a park node
        bad_user = User.objects.create_user(email="marketingexecutive1@horizon.local", password="pass")
        bad_assignment = UserRoleAssignment.objects.create(user=bad_user, role=me_role, scope_node=north)

        # Create a second "unrelated" bad email assignment at a park node
        other_bad_user = User.objects.create_user(email="some_other_bad@horizon.local", password="pass")
        other_bad_assignment = UserRoleAssignment.objects.create(
            user=other_bad_user, role=me_role, scope_node=north,
        )

        # Run the seed
        out = StringIO()
        call_command("seed_horizon_me_workflow", stdout=out)

        # The known-bad seeded assignment must be deleted
        assert not UserRoleAssignment.objects.filter(id=bad_assignment.id).exists(), (
            "Known-bad seeded user assignment should have been deleted"
        )
        # The unrelated bad-email assignment must be preserved
        assert UserRoleAssignment.objects.filter(id=other_bad_assignment.id).exists(), (
            "Unrelated bad-email ME assignment at park node was incorrectly deleted"
        )
