"""
API-level tests for the campaign endpoints.
"""
import pytest
from decimal import Decimal
from rest_framework import status
from rest_framework.test import APIClient

from apps.campaigns.models import Campaign, CampaignDocument, CampaignStatus
from apps.budgets.models import (
    BudgetCategory, BudgetSubCategory, Budget, BudgetRule,
    BudgetStatus, PeriodType, VarianceStatus,
)
from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def org(db):
    return Organization.objects.create(name="API Campaign Org", code="api-camp-org")


@pytest.fixture
def node(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/api-camp-org/hq", depth=0, is_active=True,
    )


@pytest.fixture
def entity(org, node):
    return ScopeNode.objects.create(
        org=org, parent=node, name="Entity Camp", code="entity-camp",
        node_type=NodeType.ENTITY, path="/api-camp-org/hq/entity-camp", depth=1, is_active=True,
    )


@pytest.fixture
def category(org):
    return BudgetCategory.objects.create(org=org, name="Marketing", code="marketing")


@pytest.fixture
def subcategory(category):
    return BudgetSubCategory.objects.create(category=category, name="Digital Ads", code="digital-ads")


@pytest.fixture
def admin_user(db, node, org):
    from apps.access.models import Role, UserRoleAssignment
    u = User.objects.create_user(email="apicamp@test.com", password="pass")
    role, _ = Role.objects.get_or_create(name="Campaign Admin", code="campaign-admin", org=org, defaults={"name": "Campaign Admin"})
    UserRoleAssignment.objects.create(user=u, role=role, scope_node=node)
    return u


@pytest.fixture
def entity_user(db, entity, org):
    from apps.access.models import Role, UserRoleAssignment
    u = User.objects.create_user(email="entitycamp@test.com", password="pass")
    role, _ = Role.objects.get_or_create(name="Entity Campaign Admin", code="entity-campaign-admin", org=org, defaults={"name": "Entity Campaign Admin"})
    UserRoleAssignment.objects.create(user=u, role=role, scope_node=entity)
    return u


@pytest.fixture
def no_scope_user(db):
    return User.objects.create_user(email="noscopecamp@test.com", password="pass")


@pytest.fixture
def active_budget(org, node, category, subcategory, admin_user):
    b = Budget.objects.create(
        org=org, scope_node=node, category=category, subcategory=subcategory,
        financial_year="2026-27", period_type=PeriodType.YEARLY,
        period_start="2026-04-01", period_end="2027-03-31",
        allocated_amount=Decimal("10000000.00"),
        reserved_amount=Decimal("0"), consumed_amount=Decimal("0"),
        currency="INR", status=BudgetStatus.ACTIVE, created_by=admin_user,
    )
    BudgetRule.objects.create(
        budget=b,
        warning_threshold_percent=Decimal("80.00"),
        approval_threshold_percent=Decimal("100.00"),
        hard_block_threshold_percent=Decimal("110.00"),
    )
    return b


@pytest.fixture
def entity_budget(org, entity, category, subcategory, entity_user):
    b = Budget.objects.create(
        org=org, scope_node=entity, category=category, subcategory=subcategory,
        financial_year="2026-27", period_type=PeriodType.YEARLY,
        period_start="2026-04-01", period_end="2027-03-31",
        allocated_amount=Decimal("10000000.00"),
        reserved_amount=Decimal("0"), consumed_amount=Decimal("0"),
        currency="INR", status=BudgetStatus.ACTIVE, created_by=entity_user,
    )
    BudgetRule.objects.create(
        budget=b,
        warning_threshold_percent=Decimal("80.00"),
        approval_threshold_percent=Decimal("100.00"),
        hard_block_threshold_percent=Decimal("110.00"),
    )
    return b


@pytest.fixture
def campaign(org, node, category, subcategory, admin_user):
    return Campaign.objects.create(
        org=org, scope_node=node,
        name="API Test Campaign", code="api-test-001",
        requested_amount=Decimal("500000.00"),
        created_by=admin_user,
        status=CampaignStatus.DRAFT,
    )


@pytest.fixture
def entity_campaign(org, entity, category, subcategory, entity_user, entity_budget):
    return Campaign.objects.create(
        org=org, scope_node=entity,
        name="Entity Campaign", code="entity-camp-001",
        requested_amount=Decimal("500000.00"),
        created_by=entity_user,
        status=CampaignStatus.DRAFT,
        category=category,
        subcategory=subcategory,
        budget=entity_budget,
    )


# ---------------------------------------------------------------------------
# 1. Campaign CRUD
# ---------------------------------------------------------------------------

class TestCampaignCRUD:
    def test_list_campaigns(self, api_client, admin_user, campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get("/api/v1/campaigns/")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] >= 1

    def test_create_campaign(self, api_client, admin_user, org, node):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post("/api/v1/campaigns/", {
            "org": org.id,
            "scope_node": node.id,
            "name": "New Campaign",
            "code": "new-camp-001",
            "requested_amount": "250000.00",
        })
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == "draft"
        assert response.data["name"] == "New Campaign"

    def test_retrieve_campaign(self, api_client, admin_user, campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/campaigns/{campaign.id}/")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == campaign.id

    def test_patch_campaign(self, api_client, admin_user, campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.patch(f"/api/v1/campaigns/{campaign.id}/", {
            "description": "Updated description",
        })
        assert response.status_code == status.HTTP_200_OK

    def test_delete_campaign(self, api_client, admin_user, campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.delete(f"/api/v1/campaigns/{campaign.id}/")
        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_unauthenticated_returns_401(self, api_client):
        response = api_client.get("/api/v1/campaigns/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_create_validates_subcategory_category_mismatch(self, api_client, admin_user, org, node):
        other_cat = BudgetCategory.objects.create(org=org, name="IT", code="it")
        sub = BudgetSubCategory.objects.create(category=other_cat, name="Hardware", code="hardware")
        mkt = BudgetCategory.objects.create(org=org, name="Mkt2", code="mkt2")
        api_client.force_authenticate(user=admin_user)
        response = api_client.post("/api/v1/campaigns/", {
            "org": org.id,
            "scope_node": node.id,
            "name": "Bad Campaign",
            "code": "bad-camp-001",
            "requested_amount": "100.00",
            "category": mkt.id,
            "subcategory": sub.id,
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 2. Campaign filters
# ---------------------------------------------------------------------------

class TestCampaignFilters:
    def test_filter_by_status(self, api_client, admin_user, campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get("/api/v1/campaigns/?status=draft")
        assert response.status_code == status.HTTP_200_OK
        for item in response.data["results"]:
            assert item["status"] == "draft"

    def test_filter_by_org(self, api_client, admin_user, campaign, org):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/campaigns/?org={org.id}")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] >= 1

    def test_filter_by_scope_node(self, api_client, admin_user, campaign, node):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/campaigns/?scope_node={node.id}")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] >= 1


class TestCampaignScopeVisibilityAndAuthority:
    def test_company_scope_user_can_see_child_entity_campaign(self, api_client, admin_user, entity_campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.get("/api/v1/campaigns/")
        assert response.status_code == status.HTTP_200_OK
        assert any(item["id"] == entity_campaign.id for item in response.data["results"])

    def test_no_scope_user_sees_no_campaigns(self, api_client, no_scope_user, campaign, entity_campaign):
        api_client.force_authenticate(user=no_scope_user)
        response = api_client.get("/api/v1/campaigns/")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 0

    def test_company_scope_user_cannot_patch_child_entity_campaign(self, api_client, admin_user, entity_campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.patch(
            f"/api/v1/campaigns/{entity_campaign.id}/",
            {"description": "Should fail"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_company_scope_user_cannot_submit_child_entity_campaign_budget(self, api_client, admin_user, entity_campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/campaigns/{entity_campaign.id}/submit-budget/")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_company_scope_user_cannot_review_child_entity_campaign_variance(self, api_client, admin_user, entity_user, org, entity, entity_budget):
        camp = Campaign.objects.create(
            org=org, scope_node=entity,
            name="Entity Variance Campaign", code="entity-var-001",
            requested_amount=Decimal("10500000.00"),
            created_by=entity_user,
            status=CampaignStatus.DRAFT,
            budget=entity_budget,
            category=entity_budget.category,
            subcategory=entity_budget.subcategory,
        )
        from apps.campaigns.services import submit_campaign_for_budget
        submit_campaign_for_budget(camp, submitted_by=entity_user)
        camp.refresh_from_db()
        assert camp.status == CampaignStatus.BUDGET_VARIANCE_PENDING

        api_client.force_authenticate(user=admin_user)
        response = api_client.post(
            f"/api/v1/campaigns/{camp.id}/review-budget-variance/",
            {"decision": "approved"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_company_scope_user_cannot_cancel_child_entity_campaign(self, api_client, admin_user, entity_campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/campaigns/{entity_campaign.id}/cancel/")
        assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# 3. submit-budget endpoint
# ---------------------------------------------------------------------------

class TestSubmitBudgetEndpoint:
    def test_submit_no_budget_returns_200(self, api_client, admin_user, campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/campaigns/{campaign.id}/submit-budget/")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == "no_budget_linked"
        assert response.data["campaign"]["status"] == "pending_workflow"

    def test_submit_with_budget_below_warning(self, api_client, admin_user, org, node, active_budget):
        camp = Campaign.objects.create(
            org=org, scope_node=node,
            name="Budgeted Campaign", code="budgeted-001",
            requested_amount=Decimal("500000.00"),
            created_by=admin_user, status=CampaignStatus.DRAFT,
            budget=active_budget,
        )
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/campaigns/{camp.id}/submit-budget/")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == "reserved"
        assert response.data["campaign"]["status"] == "pending_workflow"

    def test_submit_wrong_status_returns_400(self, api_client, admin_user, campaign):
        campaign.status = CampaignStatus.PENDING_WORKFLOW
        campaign.save()
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/campaigns/{campaign.id}/submit-budget/")
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 4. review-budget-variance endpoint
# ---------------------------------------------------------------------------

class TestReviewBudgetVarianceEndpoint:
    def _make_variance_campaign(self, org, node, admin_user, active_budget):
        camp = Campaign.objects.create(
            org=org, scope_node=node,
            name="Variance Campaign", code="var-001",
            requested_amount=Decimal("10500000.00"),
            created_by=admin_user, status=CampaignStatus.DRAFT,
            budget=active_budget,
        )
        from apps.campaigns.services import submit_campaign_for_budget
        submit_campaign_for_budget(camp, submitted_by=admin_user)
        camp.refresh_from_db()
        return camp

    def test_approve_variance(self, api_client, admin_user, org, node, active_budget):
        camp = self._make_variance_campaign(org, node, admin_user, active_budget)
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(
            f"/api/v1/campaigns/{camp.id}/review-budget-variance/",
            {"decision": "approved"},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == "pending_workflow"

    def test_reject_variance(self, api_client, admin_user, org, node, active_budget):
        camp = self._make_variance_campaign(org, node, admin_user, active_budget)
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(
            f"/api/v1/campaigns/{camp.id}/review-budget-variance/",
            {"decision": "rejected", "review_note": "Not justified"},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == "rejected"

    def test_invalid_decision_returns_400(self, api_client, admin_user, org, node, active_budget):
        camp = self._make_variance_campaign(org, node, admin_user, active_budget)
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(
            f"/api/v1/campaigns/{camp.id}/review-budget-variance/",
            {"decision": "maybe"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 5. cancel endpoint
# ---------------------------------------------------------------------------

class TestCancelEndpoint:
    def test_cancel_draft_campaign(self, api_client, admin_user, campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/campaigns/{campaign.id}/cancel/")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == "cancelled"

    def test_cancel_with_note(self, api_client, admin_user, campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/campaigns/{campaign.id}/cancel/", {
            "note": "No longer needed",
        })
        assert response.status_code == status.HTTP_200_OK

    def test_cancel_approved_returns_400(self, api_client, admin_user, campaign):
        campaign.status = CampaignStatus.FINANCE_APPROVED
        campaign.save()
        api_client.force_authenticate(user=admin_user)
        response = api_client.post(f"/api/v1/campaigns/{campaign.id}/cancel/")
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 6. CampaignDocument CRUD basics
# ---------------------------------------------------------------------------

class TestCampaignDocumentCRUD:
    def test_create_document(self, api_client, admin_user, campaign):
        api_client.force_authenticate(user=admin_user)
        response = api_client.post("/api/v1/campaigns/documents/", {
            "campaign": campaign.id,
            "title": "Brief.pdf",
            "file_url": "https://storage.example.com/brief.pdf",
            "document_type": "brief",
        })
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["title"] == "Brief.pdf"

    def test_list_documents(self, api_client, admin_user, campaign):
        CampaignDocument.objects.create(
            campaign=campaign,
            title="Doc1",
            file_url="https://storage.example.com/doc1.pdf",
            uploaded_by=admin_user,
        )
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/campaigns/documents/?campaign={campaign.id}")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] >= 1

    def test_retrieve_document(self, api_client, admin_user, campaign):
        doc = CampaignDocument.objects.create(
            campaign=campaign,
            title="Doc2",
            file_url="https://storage.example.com/doc2.pdf",
            uploaded_by=admin_user,
        )
        api_client.force_authenticate(user=admin_user)
        response = api_client.get(f"/api/v1/campaigns/documents/{doc.id}/")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == doc.id

    def test_delete_document(self, api_client, admin_user, campaign):
        doc = CampaignDocument.objects.create(
            campaign=campaign,
            title="Doc3",
            file_url="https://storage.example.com/doc3.pdf",
            uploaded_by=admin_user,
        )
        api_client.force_authenticate(user=admin_user)
        response = api_client.delete(f"/api/v1/campaigns/documents/{doc.id}/")
        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_patch_document_not_allowed(self, api_client, admin_user, campaign):
        """Documents are immutable — no PATCH."""
        doc = CampaignDocument.objects.create(
            campaign=campaign, title="Doc4",
            file_url="https://storage.example.com/doc4.pdf",
            uploaded_by=admin_user,
        )
        api_client.force_authenticate(user=admin_user)
        response = api_client.patch(f"/api/v1/campaigns/documents/{doc.id}/", {
            "title": "Changed",
        })
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
