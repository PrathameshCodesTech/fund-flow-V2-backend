"""
Option 2 Policy Compliance Tests

Tests that prove the intended access policy is enforced consistently:
  - VISIBILITY = subtree: user sees records in their direct + descendant scopes
  - AUTHORITY  = explicit: user can mutate only where they have a direct assignment
  - No-scope users get no visibility or authority
"""
import pytest
from decimal import Decimal
from rest_framework import status as http_status
from rest_framework.test import APIClient

from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User
from apps.access.models import Role, UserRoleAssignment
from apps.budgets.models import (
    Budget, BudgetCategory, BudgetSubCategory, BudgetStatus, PeriodType,
)
from apps.campaigns.models import Campaign, CampaignStatus
from apps.vendors.models import Vendor, MarketingStatus, OperationalStatus
from apps.modules.models import ModuleActivation


# ---------------------------------------------------------------------------
# Shared Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Policy Org", code="pol-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="pol-hq",
        node_type=NodeType.COMPANY, path="/pol-org/pol-hq", depth=0, is_active=True,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="pol-ea",
        node_type=NodeType.ENTITY, path="/pol-org/pol-hq/pol-ea", depth=1, is_active=True,
    )


@pytest.fixture
def role(org):
    return Role.objects.create(name="Policy Role", code="pol-role", org=org)


@pytest.fixture
def company_user(db, company, role):
    """User with direct assignment ONLY at company level."""
    user = User.objects.create_user(email="company@pol.com", password="pass")
    UserRoleAssignment.objects.create(user=user, role=role, scope_node=company)
    return user


@pytest.fixture
def entity_user(db, entity, role):
    """User with direct assignment ONLY at entity level."""
    user = User.objects.create_user(email="entity@pol.com", password="pass")
    UserRoleAssignment.objects.create(user=user, role=role, scope_node=entity)
    return user


@pytest.fixture
def no_scope_user(db):
    """User with NO scope assignments at all."""
    return User.objects.create_user(email="noscope@pol.com", password="pass")


@pytest.fixture
def category(org):
    return BudgetCategory.objects.create(org=org, name="Policy Cat", code="pol-cat")


@pytest.fixture
def subcategory(category):
    return BudgetSubCategory.objects.create(category=category, name="Sub", code="pol-sub")


@pytest.fixture
def company_budget(org, company, category, subcategory, company_user):
    return Budget.objects.create(
        org=org, scope_node=company, category=category, subcategory=subcategory,
        financial_year="2026-27", period_type=PeriodType.YEARLY,
        period_start="2026-04-01", period_end="2027-03-31",
        allocated_amount=Decimal("1000000.00"), reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"), currency="INR",
        status=BudgetStatus.ACTIVE, created_by=company_user,
    )


@pytest.fixture
def entity_budget(org, entity, category, subcategory, entity_user):
    return Budget.objects.create(
        org=org, scope_node=entity, category=category, subcategory=subcategory,
        financial_year="2026-27", period_type=PeriodType.YEARLY,
        period_start="2026-04-01", period_end="2027-03-31",
        allocated_amount=Decimal("500000.00"), reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"), currency="INR",
        status=BudgetStatus.ACTIVE, created_by=entity_user,
    )


@pytest.fixture
def entity_campaign(org, entity, category, entity_user, entity_budget):
    return Campaign.objects.create(
        org=org, scope_node=entity, name="Entity Camp", code="ec-pol",
        requested_amount=Decimal("10000.00"), currency="INR",
        status=CampaignStatus.DRAFT, created_by=entity_user,
        category=category,
    )


@pytest.fixture
def entity_vendor(org, entity):
    return Vendor.objects.create(
        org=org, scope_node=entity,
        vendor_name="Vendor A", email="vendora@pol.com",
        sap_vendor_id="SAP001",
        marketing_status=MarketingStatus.PENDING,
        operational_status=OperationalStatus.WAITING_MARKETING_APPROVAL,
    )


# ---------------------------------------------------------------------------
# Policy 1: Visibility = Subtree
# ---------------------------------------------------------------------------

class TestSubtreeVisibility:
    """Company-level user can see records in descendant entity scopes."""

    def test_company_user_sees_entity_budget(
        self, api_client, company_user, entity_budget
    ):
        """Visibility: company user can list/read entity budgets (descendant scope)."""
        api_client.force_authenticate(user=company_user)
        resp = api_client.get("/api/v1/budgets/")
        assert resp.status_code == http_status.HTTP_200_OK
        ids = [b["id"] for b in resp.data["results"]]
        assert entity_budget.id in ids

    def test_company_user_can_retrieve_entity_budget(
        self, api_client, company_user, entity_budget
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.get(f"/api/v1/budgets/{entity_budget.id}/")
        assert resp.status_code == http_status.HTTP_200_OK

    def test_company_user_sees_entity_campaign(
        self, api_client, company_user, entity_campaign
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.get("/api/v1/campaigns/")
        assert resp.status_code == http_status.HTTP_200_OK
        ids = [c["id"] for c in resp.data["results"]]
        assert entity_campaign.id in ids

    def test_entity_user_does_not_see_company_budget(
        self, api_client, entity_user, company_budget
    ):
        """Entity user cannot see records at a parent scope — no upward visibility."""
        api_client.force_authenticate(user=entity_user)
        resp = api_client.get("/api/v1/budgets/")
        assert resp.status_code == http_status.HTTP_200_OK
        ids = [b["id"] for b in resp.data["results"]]
        assert company_budget.id not in ids

    def test_no_scope_user_sees_nothing(
        self, api_client, no_scope_user, entity_budget, company_budget
    ):
        """User with no assignments sees no records anywhere."""
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.get("/api/v1/budgets/")
        assert resp.status_code == http_status.HTTP_200_OK
        assert len(resp.data["results"]) == 0


# ---------------------------------------------------------------------------
# Policy 2: Authority = Explicit Only (no inherited mutations)
# ---------------------------------------------------------------------------

class TestExplicitAuthority:
    """Company-level user cannot mutate entity-scoped records."""

    def test_company_user_cannot_update_entity_budget(
        self, api_client, company_user, entity_budget
    ):
        """Authority: company user cannot PATCH entity budget (no direct assignment at entity)."""
        api_client.force_authenticate(user=company_user)
        resp = api_client.patch(
            f"/api/v1/budgets/{entity_budget.id}/",
            {"financial_year": "2027-28"},
        )
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_company_user_cannot_delete_entity_budget(
        self, api_client, company_user, entity_budget
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.delete(f"/api/v1/budgets/{entity_budget.id}/")
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_company_user_cannot_create_budget_at_entity(
        self, api_client, company_user, entity, org, category, subcategory
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.post("/api/v1/budgets/", {
            "org": org.id,
            "scope_node": entity.id,
            "category": category.id,
            "subcategory": subcategory.id,
            "financial_year": "2026-27",
            "period_type": PeriodType.YEARLY,
            "period_start": "2026-04-01",
            "period_end": "2027-03-31",
            "allocated_amount": "100000.00",
            "currency": "INR",
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_no_scope_user_cannot_create_budget(
        self, api_client, no_scope_user, company, org, category, subcategory
    ):
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.post("/api/v1/budgets/", {
            "org": org.id,
            "scope_node": company.id,
            "category": category.id,
            "financial_year": "2026-27",
            "period_type": PeriodType.YEARLY,
            "period_start": "2026-04-01",
            "period_end": "2027-03-31",
            "allocated_amount": "100000.00",
            "currency": "INR",
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_company_user_cannot_update_entity_campaign(
        self, api_client, company_user, entity_campaign
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.patch(
            f"/api/v1/campaigns/{entity_campaign.id}/",
            {"name": "Hacked Name"},
        )
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_entity_user_can_update_own_entity_campaign(
        self, api_client, entity_user, entity_campaign
    ):
        """Entity user CAN update a campaign at their own (direct-assignment) node."""
        api_client.force_authenticate(user=entity_user)
        resp = api_client.patch(
            f"/api/v1/campaigns/{entity_campaign.id}/",
            {"name": "Updated Name"},
        )
        assert resp.status_code == http_status.HTTP_200_OK


# ---------------------------------------------------------------------------
# Budget reserve/consume/release (high-risk actions)
# ---------------------------------------------------------------------------

class TestBudgetHighRiskActions:

    def test_company_user_cannot_reserve_entity_budget(
        self, api_client, company_user, entity_budget
    ):
        """Reserve at entity scope requires direct assignment at entity."""
        api_client.force_authenticate(user=company_user)
        resp = api_client.post(f"/api/v1/budgets/{entity_budget.id}/reserve/", {
            "amount": "1000.00",
            "source_type": "manual_adjustment",
            "source_id": "1",
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_no_scope_user_cannot_reserve_budget(
        self, api_client, no_scope_user, entity_budget
    ):
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.post(f"/api/v1/budgets/{entity_budget.id}/reserve/", {
            "amount": "1000.00",
            "source_type": "manual_adjustment",
            "source_id": "1",
        })
        # No-scope user can't even see the budget — 404, not 403
        assert resp.status_code in (http_status.HTTP_403_FORBIDDEN, http_status.HTTP_404_NOT_FOUND)

    def test_company_user_cannot_consume_entity_budget(
        self, api_client, company_user, entity_budget
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.post(f"/api/v1/budgets/{entity_budget.id}/consume/", {
            "amount": "100.00",
            "source_type": "manual_adjustment",
            "source_id": "1",
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_company_user_cannot_release_entity_budget(
        self, api_client, company_user, entity_budget
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.post(f"/api/v1/budgets/{entity_budget.id}/release/", {
            "amount": "100.00",
            "source_type": "manual_adjustment",
            "source_id": "1",
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_entity_user_can_reserve_entity_budget(
        self, api_client, entity_user, entity_budget
    ):
        """Entity user with direct assignment can reserve on their own budget."""
        api_client.force_authenticate(user=entity_user)
        resp = api_client.post(f"/api/v1/budgets/{entity_budget.id}/reserve/", {
            "amount": "1000.00",
            "source_type": "manual_adjustment",
            "source_id": "99",
        })
        # 201 Created (consumption) or 200 (variance request) — not 403
        assert resp.status_code in (http_status.HTTP_201_CREATED, http_status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Campaign submit/review/cancel (high-risk actions)
# ---------------------------------------------------------------------------

class TestCampaignHighRiskActions:

    def test_company_user_cannot_submit_budget_for_entity_campaign(
        self, api_client, company_user, entity_campaign
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.post(f"/api/v1/campaigns/{entity_campaign.id}/submit-budget/")
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_company_user_cannot_cancel_entity_campaign(
        self, api_client, company_user, entity_campaign
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.post(f"/api/v1/campaigns/{entity_campaign.id}/cancel/", {
            "note": "unauthorized cancel attempt",
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_company_user_cannot_review_budget_variance_on_entity_campaign(
        self, api_client, company_user, entity_campaign
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.post(
            f"/api/v1/campaigns/{entity_campaign.id}/review-budget-variance/",
            {"decision": "approve"},
        )
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_no_scope_user_cannot_cancel_campaign(
        self, api_client, no_scope_user, entity_campaign
    ):
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.post(f"/api/v1/campaigns/{entity_campaign.id}/cancel/", {
            "note": "unauthorized",
        })
        assert resp.status_code in (http_status.HTTP_403_FORBIDDEN, http_status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# Vendor marketing-approve/reject/send-to-finance (high-risk actions)
# ---------------------------------------------------------------------------

class TestVendorHighRiskActions:

    def test_company_user_cannot_marketing_approve_entity_vendor(
        self, api_client, company_user, entity_vendor
    ):
        """Approve vendor at entity requires direct assignment at entity."""
        api_client.force_authenticate(user=company_user)
        resp = api_client.post(
            f"/api/v1/vendors/{entity_vendor.id}/marketing-approve/",
            {"po_mandate_enabled": False},
        )
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_company_user_cannot_marketing_reject_entity_vendor(
        self, api_client, company_user, entity_vendor
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.post(
            f"/api/v1/vendors/{entity_vendor.id}/marketing-reject/",
            {"note": "rejected"},
        )
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_no_scope_user_cannot_see_vendors(
        self, api_client, no_scope_user, entity_vendor
    ):
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.get("/api/v1/vendors/")
        assert resp.status_code == http_status.HTTP_200_OK
        assert len(resp.data["results"]) == 0

    def test_entity_user_can_see_entity_vendor(
        self, api_client, entity_user, entity_vendor
    ):
        api_client.force_authenticate(user=entity_user)
        resp = api_client.get("/api/v1/vendors/")
        assert resp.status_code == http_status.HTTP_200_OK
        ids = [v["id"] for v in resp.data["results"]]
        assert entity_vendor.id in ids


# ---------------------------------------------------------------------------
# Module activation changes (high-risk)
# ---------------------------------------------------------------------------

class TestModuleActivationAuthority:

    def test_company_user_can_create_module_activation_at_company(
        self, api_client, company_user, company
    ):
        api_client.force_authenticate(user=company_user)
        resp = api_client.post("/api/v1/modules/activations/", {
            "module": "budget",
            "scope_node": company.id,
            "is_active": True,
            "override_parent": True,
        })
        assert resp.status_code == http_status.HTTP_201_CREATED

    def test_company_user_cannot_create_module_activation_at_entity(
        self, api_client, company_user, entity
    ):
        """Company user has no direct assignment at entity — cannot activate modules there."""
        api_client.force_authenticate(user=company_user)
        resp = api_client.post("/api/v1/modules/activations/", {
            "module": "campaign",
            "scope_node": entity.id,
            "is_active": True,
            "override_parent": True,
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_no_scope_user_cannot_create_module_activation(
        self, api_client, no_scope_user, company
    ):
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.post("/api/v1/modules/activations/", {
            "module": "budget",
            "scope_node": company.id,
            "is_active": True,
            "override_parent": True,
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_company_user_cannot_update_entity_activation(
        self, api_client, company_user, entity
    ):
        """Cannot patch an activation record at a scope you can't act on."""
        activation = ModuleActivation.objects.create(
            module="budget", scope_node=entity, is_active=False, override_parent=True
        )
        api_client.force_authenticate(user=company_user)
        resp = api_client.patch(
            f"/api/v1/modules/activations/{activation.id}/",
            {"is_active": True},
        )
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_no_scope_user_cannot_see_activations(
        self, api_client, no_scope_user, entity
    ):
        ModuleActivation.objects.create(
            module="budget", scope_node=entity, is_active=True, override_parent=True
        )
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.get("/api/v1/modules/activations/")
        assert resp.status_code == http_status.HTTP_200_OK
        assert len(resp.data["results"]) == 0


# ---------------------------------------------------------------------------
# Role assignment mutations (CRITICAL security control)
# ---------------------------------------------------------------------------

class TestRoleAssignmentAuthority:

    def test_company_user_can_create_role_assignment_at_company(
        self, api_client, company_user, company, role, no_scope_user
    ):
        """Company user can create role assignments at their own direct scope."""
        api_client.force_authenticate(user=company_user)
        resp = api_client.post("/api/v1/access/role-assignments/", {
            "user": no_scope_user.id,
            "role": role.id,
            "scope_node": company.id,
        })
        assert resp.status_code == http_status.HTTP_201_CREATED

    def test_company_user_cannot_create_role_assignment_at_entity(
        self, api_client, company_user, entity, role, no_scope_user
    ):
        """Company user cannot grant roles at descendant entity — explicit authority only."""
        api_client.force_authenticate(user=company_user)
        resp = api_client.post("/api/v1/access/role-assignments/", {
            "user": no_scope_user.id,
            "role": role.id,
            "scope_node": entity.id,
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_no_scope_user_cannot_create_role_assignment(
        self, api_client, no_scope_user, company, role, entity_user
    ):
        """A user with no assignments cannot grant roles to anyone."""
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.post("/api/v1/access/role-assignments/", {
            "user": entity_user.id,
            "role": role.id,
            "scope_node": company.id,
        })
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN

    def test_no_scope_user_cannot_delete_any_role_assignment(
        self, api_client, no_scope_user, entity, role, entity_user
    ):
        """No-scope user cannot delete existing role assignments."""
        assignment = UserRoleAssignment.objects.get(user=entity_user, scope_node=entity)
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.delete(f"/api/v1/access/role-assignments/{assignment.id}/")
        assert resp.status_code in (http_status.HTTP_403_FORBIDDEN, http_status.HTTP_404_NOT_FOUND)

    def test_company_user_cannot_delete_entity_role_assignment(
        self, api_client, company_user, entity, entity_user
    ):
        """Company user cannot delete a role assignment at a scope they can't act on."""
        assignment = UserRoleAssignment.objects.get(user=entity_user, scope_node=entity)
        api_client.force_authenticate(user=company_user)
        resp = api_client.delete(f"/api/v1/access/role-assignments/{assignment.id}/")
        assert resp.status_code == http_status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Workflow instances — scope-filtered visibility
# ---------------------------------------------------------------------------

class TestWorkflowInstanceVisibility:

    def test_no_scope_user_cannot_see_workflow_instances(
        self, api_client, no_scope_user, entity
    ):
        api_client.force_authenticate(user=no_scope_user)
        resp = api_client.get("/api/v1/workflow/instances/")
        assert resp.status_code == http_status.HTTP_200_OK
        assert len(resp.data["results"]) == 0

    def test_company_user_sees_entity_workflow_instances(
        self, api_client, company_user, entity_user, entity
    ):
        """Company user (has visible scope over entity) sees entity-scoped instances."""
        from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus
        template = WorkflowTemplate.objects.create(
            module="invoice", scope_node=entity, created_by=entity_user,
        )
        version = WorkflowTemplateVersion.objects.create(
            template=template, version_number=1, status=VersionStatus.PUBLISHED,
            published_by=entity_user,
        )
        instance = __import__(
            "apps.workflow.models", fromlist=["WorkflowInstance"]
        ).WorkflowInstance.objects.create(
            template_version=version,
            subject_type="invoice",
            subject_id=1,
            subject_scope_node=entity,
            started_by=entity_user,
        )
        api_client.force_authenticate(user=company_user)
        resp = api_client.get("/api/v1/workflow/instances/")
        assert resp.status_code == http_status.HTTP_200_OK
        ids = [i["id"] for i in resp.data["results"]]
        assert instance.id in ids

    def test_entity_user_does_not_see_company_workflow_instances(
        self, api_client, entity_user, company_user, company
    ):
        """Entity user cannot see workflow instances at parent company scope."""
        from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus, WorkflowInstance
        template = WorkflowTemplate.objects.create(
            module="invoice", scope_node=company, created_by=company_user,
        )
        version = WorkflowTemplateVersion.objects.create(
            template=template, version_number=1, status=VersionStatus.PUBLISHED,
            published_by=company_user,
        )
        instance = WorkflowInstance.objects.create(
            template_version=version,
            subject_type="invoice",
            subject_id=2,
            subject_scope_node=company,
            started_by=company_user,
        )
        api_client.force_authenticate(user=entity_user)
        resp = api_client.get("/api/v1/workflow/instances/")
        assert resp.status_code == http_status.HTTP_200_OK
        ids = [i["id"] for i in resp.data["results"]]
        assert instance.id not in ids
