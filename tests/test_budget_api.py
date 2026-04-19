"""
API-level tests for budgets app — all CRUD endpoints + runtime actions.
"""
import pytest
from decimal import Decimal
from rest_framework.test import APIClient
from rest_framework import status

from apps.budgets.models import (
    BudgetCategory, BudgetSubCategory, Budget, BudgetRule,
    BudgetConsumption, BudgetVarianceRequest,
    BudgetStatus, PeriodType, ConsumptionType, ConsumptionStatus,
    VarianceStatus, SourceType,
)
from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Budget API Org", code="bapi-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq-api",
        node_type=NodeType.COMPANY, path="/bapi-org/hq-api", depth=0, is_active=True,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity API", code="entity-api",
        node_type=NodeType.ENTITY, path="/bapi-org/hq-api/entity-api", depth=1, is_active=True,
    )


@pytest.fixture
def category(org, company):
    return BudgetCategory.objects.create(org=org, name="Marketing", code="mkt-api")


@pytest.fixture
def subcategory(category):
    return BudgetSubCategory.objects.create(category=category, name="Digital Ads", code="dig-api")


from apps.access.models import Role, UserRoleAssignment


@pytest.fixture
def admin_user(db, company, org):
    user = User.objects.create_user(email="budapi@bud.com", password="pass")
    role, _ = Role.objects.get_or_create(name="Budget Admin", code="budget-admin", org=org, defaults={"name": "Budget Admin"})
    UserRoleAssignment.objects.create(user=user, role=role, scope_node=company)
    return user


@pytest.fixture
def entity_user(db, entity, org):
    user = User.objects.create_user(email="entitybud@bud.com", password="pass")
    role, _ = Role.objects.get_or_create(name="Entity Budget Admin", code="entity-budget-admin", org=org, defaults={"name": "Entity Budget Admin"})
    UserRoleAssignment.objects.create(user=user, role=role, scope_node=entity)
    return user


@pytest.fixture
def no_scope_user(db):
    return User.objects.create_user(email="noscopebud@bud.com", password="pass")


@pytest.fixture
def budget(org, company, category, subcategory, admin_user):
    return Budget.objects.create(
        org=org,
        scope_node=company,
        category=category,
        subcategory=subcategory,
        financial_year="2026-27",
        period_type=PeriodType.YEARLY,
        period_start="2026-04-01",
        period_end="2027-03-31",
        allocated_amount=Decimal("50000000.00"),
        reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"),
        currency="INR",
        status=BudgetStatus.ACTIVE,
        created_by=admin_user,
    )


@pytest.fixture
def entity_budget(org, entity, category, subcategory, entity_user):
    return Budget.objects.create(
        org=org,
        scope_node=entity,
        category=category,
        subcategory=subcategory,
        financial_year="2026-27",
        period_type=PeriodType.YEARLY,
        period_start="2026-04-01",
        period_end="2027-03-31",
        allocated_amount=Decimal("25000000.00"),
        reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"),
        currency="INR",
        status=BudgetStatus.ACTIVE,
        created_by=entity_user,
    )


@pytest.fixture
def budget_rule(budget):
    return BudgetRule.objects.create(
        budget=budget,
        warning_threshold_percent=Decimal("80.00"),
        approval_threshold_percent=Decimal("100.00"),
        hard_block_threshold_percent=Decimal("110.00"),
    )


# ---------------------------------------------------------------------------
# Category CRUD
# ---------------------------------------------------------------------------

class TestBudgetCategoryAPI:
    def test_list_categories(self, api_client, admin_user, category):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get("/api/v1/budgets/categories/")
        assert response.status_code == status.HTTP_200_OK
        assert any(c["code"] == "mkt-api" for c in response.data["results"])

    def test_create_category(self, api_client, admin_user, org):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post("/api/v1/budgets/categories/", {
            "org": org.id,
            "name": "Finance",
            "code": "fin-api",
        })
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["name"] == "Finance"

    def test_filter_by_org(self, api_client, admin_user, category, org):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/budgets/categories/?org={org.id}")
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) >= 1

    def test_filter_by_is_active(self, api_client, admin_user, category):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get("/api/v1/budgets/categories/?is_active=true")
        assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# SubCategory CRUD
# ---------------------------------------------------------------------------

class TestBudgetSubCategoryAPI:
    def test_list_subcategories(self, api_client, admin_user, subcategory):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get("/api/v1/budgets/subcategories/")
        assert response.status_code == status.HTTP_200_OK
        assert any(c["code"] == "dig-api" for c in response.data["results"])

    def test_create_subcategory(self, api_client, admin_user, category):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post("/api/v1/budgets/subcategories/", {
            "category": category.id,
            "name": "Events",
            "code": "evt-api",
        })
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["name"] == "Events"

    def test_filter_by_category(self, api_client, admin_user, subcategory, category):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/budgets/subcategories/?category={category.id}")
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) >= 1


# ---------------------------------------------------------------------------
# Budget CRUD
# ---------------------------------------------------------------------------

class TestBudgetAPI:
    def test_list_budgets(self, api_client, admin_user, budget):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get("/api/v1/budgets/")
        assert response.status_code == status.HTTP_200_OK
        assert any(b["allocated_amount"] == "50000000.00" for b in response.data["results"])

    def test_create_budget(self, api_client, admin_user, org, company, category, subcategory):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post("/api/v1/budgets/", {
            "org": org.id,
            "scope_node": company.id,
            "category": category.id,
            "subcategory": subcategory.id,
            "financial_year": "2026-27",
            "period_type": "yearly",
            "period_start": "2026-04-01",
            "period_end": "2027-03-31",
            "allocated_amount": "10000000.00",
            "currency": "INR",
            "status": "draft",
        })
        assert response.status_code == status.HTTP_201_CREATED
        assert Decimal(response.data["allocated_amount"]) == Decimal("10000000.00")

    def test_retrieve_budget(self, api_client, admin_user, budget):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/budgets/{budget.id}/")
        assert response.status_code == status.HTTP_200_OK
        assert Decimal(response.data["allocated_amount"]) == Decimal("50000000.00")
        assert "available_amount" in response.data
        assert "utilization_percent" in response.data

    def test_budget_available_amount_computed(self, api_client, admin_user, budget):
        budget.reserved_amount = Decimal("10000000.00")
        budget.consumed_amount = Decimal("5000000.00")
        budget.save()
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/budgets/{budget.id}/")
        assert Decimal(response.data["available_amount"]) == Decimal("35000000.00")

    def test_filter_budgets_by_org(self, api_client, admin_user, budget, org):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/budgets/?org={org.id}")
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) >= 1

    def test_filter_budgets_by_status(self, api_client, admin_user, budget):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get("/api/v1/budgets/?status=active")
        assert response.status_code == status.HTTP_200_OK


class TestBudgetScopeVisibilityAndAuthority:
    def test_company_scope_user_can_see_child_entity_budget(self, api_client, admin_user, entity_budget):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get("/api/v1/budgets/")
        assert response.status_code == status.HTTP_200_OK
        assert any(item["id"] == entity_budget.id for item in response.data["results"])

    def test_no_scope_user_sees_no_budgets(self, api_client, no_scope_user, budget, entity_budget):
        api_client.force_authenticate(user=no_scope_user)
        response = api_client.get("/api/v1/budgets/")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 0

    def test_company_scope_user_cannot_patch_child_entity_budget(self, api_client, admin_user, entity_budget):
        api_client.force_authenticate(user=admin_user)
        response = api_client.patch(
            f"/api/v1/budgets/{entity_budget.id}/",
            {"allocated_amount": "26000000.00"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_company_scope_user_cannot_reserve_child_entity_budget(self, api_client, admin_user, entity_budget):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(
            f"/api/v1/budgets/{entity_budget.id}/reserve/",
            {"amount": "1000000.00", "source_type": "campaign", "source_id": "camp-entity-001"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_company_scope_user_cannot_consume_child_entity_budget(self, api_client, admin_user, entity_user, entity_budget):
        api_client.force_authenticate(user=entity_user)
        reserve_response = api_client.post(
            f"/api/v1/budgets/{entity_budget.id}/reserve/",
            {"amount": "1000000.00", "source_type": "campaign", "source_id": "camp-entity-001"},
        )
        assert reserve_response.status_code == status.HTTP_201_CREATED

        api_client.force_authenticate(user=admin_user)
        response = api_client.post(
            f"/api/v1/budgets/{entity_budget.id}/consume/",
            {"amount": "100000.00", "source_type": "campaign", "source_id": "camp-entity-001"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_company_scope_user_cannot_release_child_entity_budget(self, api_client, admin_user, entity_user, entity_budget):
        api_client.force_authenticate(user=entity_user)
        reserve_response = api_client.post(
            f"/api/v1/budgets/{entity_budget.id}/reserve/",
            {"amount": "1000000.00", "source_type": "campaign", "source_id": "camp-entity-002"},
        )
        assert reserve_response.status_code == status.HTTP_201_CREATED

        api_client.force_authenticate(user=admin_user)
        response = api_client.post(
            f"/api/v1/budgets/{entity_budget.id}/release/",
            {"amount": "100000.00", "source_type": "campaign", "source_id": "camp-entity-002"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Reserve action
# ---------------------------------------------------------------------------

class TestReserveBudgetAPI:
    def test_reserve_below_threshold_returns_201(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "10000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
            "note": "Campaign launch",
        })
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == "reserved"
        assert response.data["consumption"] is not None

    def test_reserve_above_approval_threshold_returns_200_with_variance(
        self, api_client, admin_user, budget,
    ):
        # Default approval=100%. First reserve 39M → 78% (normal reservation).
        reserve_url = f"/api/v1/budgets/{budget.id}/reserve/"
        api_client.force_authenticate(user=admin_user)
        api_client.post(reserve_url, {
            "amount": "39000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        # Now reserve 12M more → 102% projected (above 100% approval)
        response = api_client.post(reserve_url, {
            "amount": "12000000.00",
            "source_type": "invoice",
            "source_id": "inv-api-001",
        })
        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == "variance_required"
        assert response.data["variance_request"] is not None

    def test_reserve_above_hard_block_returns_400(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "56000000.00",
            "source_type": "campaign",
            "source_id": "camp-big",
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "hard block" in response.data["detail"].lower()

    def test_reserve_zero_amount_returns_400(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "0",
            "source_type": "campaign",
            "source_id": "camp-zero",
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_reserve_inactive_budget_returns_400(
        self, api_client, admin_user, budget,
    ):
        budget.status = BudgetStatus.DRAFT
        budget.save()
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "1000000.00",
            "source_type": "campaign",
            "source_id": "camp-inactive",
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Consume action
# ---------------------------------------------------------------------------

class TestConsumeBudgetAPI:
    def test_consume_returns_201(
        self, api_client, admin_user, budget,
    ):
        # First reserve
        api_client.force_authenticate(user=admin_user)
        api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "10000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        # Then consume
        response = api_client.post(f"/api/v1/budgets/{budget.id}/consume/", {
            "amount": "5000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
            "note": "Actual spend",
        })
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == "consumed"
        assert response.data["consumption"] is not None

    def test_consume_more_than_reserved_returns_400(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        # Reserve only 1M
        api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "1000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        # Try to consume 5M
        response = api_client.post(f"/api/v1/budgets/{budget.id}/consume/", {
            "amount": "5000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Release action
# ---------------------------------------------------------------------------

class TestReleaseBudgetAPI:
    def test_release_returns_201(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        # Reserve first
        api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "10000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        # Release 3M
        response = api_client.post(f"/api/v1/budgets/{budget.id}/release/", {
            "amount": "3000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
            "note": "Campaign cancelled",
        })
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == "released"

    def test_release_more_than_reserved_returns_400(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "1000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        response = api_client.post(f"/api/v1/budgets/{budget.id}/release/", {
            "amount": "5000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# BudgetRule API
# ---------------------------------------------------------------------------

class TestBudgetRuleAPI:
    def test_create_rule(self, api_client, admin_user, budget):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post("/api/v1/budgets/rules/", {
            "budget": budget.id,
            "warning_threshold_percent": "80.00",
            "approval_threshold_percent": "100.00",
            "hard_block_threshold_percent": "110.00",
        })
        assert response.status_code == status.HTTP_201_CREATED
        assert Decimal(response.data["warning_threshold_percent"]) == Decimal("80.00")

    def test_rule_validation_warning_ge_approval(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post("/api/v1/budgets/rules/", {
            "budget": budget.id,
            "warning_threshold_percent": "90.00",
            "approval_threshold_percent": "80.00",
            "hard_block_threshold_percent": "110.00",
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_rule_validation_approval_gt_hard_block(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post("/api/v1/budgets/rules/", {
            "budget": budget.id,
            "warning_threshold_percent": "70.00",
            "approval_threshold_percent": "120.00",
            "hard_block_threshold_percent": "110.00",
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_filter_rules_by_budget(
        self, api_client, admin_user, budget_rule,
    ):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/budgets/rules/?budget={budget_rule.budget.id}")
        assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# BudgetConsumption (read-only)
# ---------------------------------------------------------------------------

class TestBudgetConsumptionAPI:
    def test_list_consumptions(
        self, api_client, admin_user, budget,
    ):
        # Create a reservation first
        api_client.force_authenticate(user=admin_user)
        api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "10000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        response = api_client.get("/api/v1/budgets/consumptions/")
        assert response.status_code == status.HTTP_200_OK

    def test_filter_consumptions_by_budget(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "10000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        response = api_client.get(f"/api/v1/budgets/consumptions/?budget={budget.id}")
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) >= 1


# ---------------------------------------------------------------------------
# VarianceRequest API + review action
# ---------------------------------------------------------------------------

class TestBudgetVarianceRequestAPI:
    def test_list_variance_requests(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        # Push into variance range
        api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "39000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        api_client.post(f"/api/v1/budgets/{budget.id}/reserve/", {
            "amount": "2000000.00",
            "source_type": "invoice",
            "source_id": "inv-api-001",
        })
        response = api_client.get("/api/v1/budgets/variance-requests/")
        assert response.status_code == status.HTTP_200_OK

    def test_approve_variance(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        reserve_url = f"/api/v1/budgets/{budget.id}/reserve/"
        # 39M → 78% (below 100% approval)
        api_client.post(reserve_url, {
            "amount": "39000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        # 12M more → 102% (above 100% approval) → variance_required
        resp = api_client.post(reserve_url, {
            "amount": "12000000.00",
            "source_type": "invoice",
            "source_id": "inv-api-001",
        })
        variance_id = resp.data["variance_request"]["id"]

        review_resp = api_client.post(
            f"/api/v1/budgets/variance-requests/{variance_id}/review/",
            {"decision": "approved", "review_note": "Approved."},
        )
        assert review_resp.status_code == status.HTTP_200_OK
        assert review_resp.data["status"] == "approved"

    def test_reject_variance(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        reserve_url = f"/api/v1/budgets/{budget.id}/reserve/"
        api_client.post(reserve_url, {
            "amount": "39000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        resp = api_client.post(reserve_url, {
            "amount": "12000000.00",
            "source_type": "invoice",
            "source_id": "inv-api-001",
        })
        variance_id = resp.data["variance_request"]["id"]

        review_resp = api_client.post(
            f"/api/v1/budgets/variance-requests/{variance_id}/review/",
            {"decision": "rejected", "review_note": "Rejected."},
        )
        assert review_resp.status_code == status.HTTP_200_OK
        assert review_resp.data["status"] == "rejected"

    def test_review_non_pending_raises(
        self, api_client, admin_user, budget,
    ):
        api_client.force_authenticate(user=admin_user)
        reserve_url = f"/api/v1/budgets/{budget.id}/reserve/"
        api_client.post(reserve_url, {
            "amount": "39000000.00",
            "source_type": "campaign",
            "source_id": "camp-api-001",
        })
        resp = api_client.post(reserve_url, {
            "amount": "12000000.00",
            "source_type": "invoice",
            "source_id": "inv-api-001",
        })
        variance_id = resp.data["variance_request"]["id"]

        # Approve first
        api_client.post(
            f"/api/v1/budgets/variance-requests/{variance_id}/review/",
            {"decision": "approved"},
        )
        # Try to approve again
        again_resp = api_client.post(
            f"/api/v1/budgets/variance-requests/{variance_id}/review/",
            {"decision": "approved"},
        )
        assert again_resp.status_code == status.HTTP_400_BAD_REQUEST
