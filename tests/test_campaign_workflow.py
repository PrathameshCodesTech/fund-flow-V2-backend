"""
Campaign → Workflow integration tests.

Covers:
- from-campaign endpoint (success + failure paths)
- campaign status sync on workflow state transitions (ACTIVE/APPROVED/REJECTED)
- invoice workflow behaviour unchanged after refactor
"""
import pytest
from rest_framework import status as http_status
from rest_framework.test import APIClient

from apps.campaigns.models import Campaign, CampaignStatus
from apps.campaigns.services import (
    create_campaign_workflow_draft,
    CampaignStateError,
    ModuleInactiveError,
    WorkflowNotConfiguredError,
)
from apps.core.models import Organization, ScopeNode, NodeType
from apps.access.models import Role, UserRoleAssignment
from apps.users.models import User
from apps.workflow.models import (
    WorkflowTemplate, WorkflowTemplateVersion, StepGroup, WorkflowStep,
    WorkflowInstance, InstanceStatus, GroupStatus, StepStatus,
    VersionStatus, ParallelMode, RejectionAction, ScopeResolutionPolicy,
)
from apps.workflow.services import (
    create_workflow_instance_draft,
    activate_workflow_instance,
    approve_workflow_step,
    reject_workflow_step,
)
from apps.modules.models import ModuleActivation, ModuleType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="CW Org", code="cw-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/cw-org/hq", depth=0, is_active=True,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/cw-org/hq/ea", depth=1, is_active=True,
    )


@pytest.fixture
def actor(db):
    return User.objects.create_user(email="camp-actor@example.com", password="pass")


@pytest.fixture
def approver(db):
    return User.objects.create_user(email="camp-approver@example.com", password="pass")


@pytest.fixture
def approver_role(org):
    return Role.objects.create(org=org, name="Campaign Approver", code="camp-approver")


@pytest.fixture
def _approver_assigned(approver, approver_role, entity):
    UserRoleAssignment.objects.create(user=approver, role=approver_role, scope_node=entity)


@pytest.fixture
def campaign_module_active(entity):
    return ModuleActivation.objects.create(
        module=ModuleType.CAMPAIGN, scope_node=entity,
        is_active=True, override_parent=True,
    )


@pytest.fixture
def campaign_template(entity, actor):
    return WorkflowTemplate.objects.create(
        name="Campaign WF", module="campaign", scope_node=entity, created_by=actor,
    )


@pytest.fixture
def published_version(campaign_template):
    return WorkflowTemplateVersion.objects.create(
        template=campaign_template, version_number=1, status=VersionStatus.PUBLISHED,
    )


@pytest.fixture
def single_step_version(published_version, approver_role, entity, approver):
    """Published version with one group / one step — approver auto-assigned."""
    g = StepGroup.objects.create(
        template_version=published_version, name="Review", display_order=1,
        parallel_mode=ParallelMode.SINGLE, on_rejection_action=RejectionAction.TERMINATE,
    )
    WorkflowStep.objects.create(
        group=g, name="Review Step", required_role=approver_role,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1, default_user=approver,
    )
    return published_version


def _make_pending_campaign(entity, actor, org=None):
    return Campaign.objects.create(
        org=org, scope_node=entity,
        name="CW Campaign", code="cw-001",
        requested_amount="500000.00",
        created_by=actor,
        status=CampaignStatus.PENDING_WORKFLOW,
    )


# ---------------------------------------------------------------------------
# 1. create_campaign_workflow_draft creates a DRAFT instance
# ---------------------------------------------------------------------------

class TestCreateCampaignWorkflowDraft:
    def test_creates_draft_for_pending_workflow_campaign(
        self, entity, actor, org, campaign_module_active, single_step_version, _approver_assigned,
    ):
        campaign = _make_pending_campaign(entity, actor, org)
        instance = create_campaign_workflow_draft(campaign, started_by=actor)
        assert instance.pk is not None
        assert instance.status == InstanceStatus.DRAFT
        assert instance.subject_type == "campaign"
        assert instance.subject_id == campaign.pk

    def test_campaign_remains_pending_workflow_after_draft_creation(
        self, entity, actor, org, campaign_module_active, single_step_version, _approver_assigned,
    ):
        """Campaign status must NOT change when workflow is only a draft."""
        campaign = _make_pending_campaign(entity, actor, org)
        create_campaign_workflow_draft(campaign, started_by=actor)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.PENDING_WORKFLOW


# ---------------------------------------------------------------------------
# 2. from-campaign API rejects campaign not in pending_workflow
# ---------------------------------------------------------------------------

class TestFromCampaignEndpoint:
    def test_from_campaign_creates_draft(
        self, entity, actor, org, campaign_module_active, single_step_version, _approver_assigned,
    ):
        campaign = _make_pending_campaign(entity, actor, org)
        client = APIClient()
        client.force_authenticate(user=actor)
        response = client.post("/api/v1/workflow/instances/from-campaign/", {
            "campaign_id": campaign.id,
        })
        assert response.status_code == http_status.HTTP_201_CREATED
        assert response.data["subject_type"] == "campaign"
        assert response.data["subject_id"] == campaign.id
        assert response.data["status"] == InstanceStatus.DRAFT

    def test_from_campaign_rejects_non_pending_workflow_status(
        self, entity, actor, org,
    ):
        campaign = Campaign.objects.create(
            org=org, scope_node=entity,
            name="Draft Camp", code="draft-001",
            requested_amount="100.00",
            created_by=actor,
            status=CampaignStatus.DRAFT,
        )
        client = APIClient()
        client.force_authenticate(user=actor)
        response = client.post("/api/v1/workflow/instances/from-campaign/", {
            "campaign_id": campaign.id,
        })
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        assert "pending_workflow" in response.data["detail"]

    def test_from_campaign_missing_campaign_id_returns_400(self, entity, actor):
        client = APIClient()
        client.force_authenticate(user=actor)
        response = client.post("/api/v1/workflow/instances/from-campaign/", {})
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST

    def test_from_campaign_nonexistent_returns_404(self, entity, actor):
        client = APIClient()
        client.force_authenticate(user=actor)
        response = client.post("/api/v1/workflow/instances/from-campaign/", {
            "campaign_id": 999999,
        })
        assert response.status_code == http_status.HTTP_404_NOT_FOUND

    def test_from_campaign_activate_true_activates_draft(
        self, entity, actor, org, campaign_module_active, single_step_version, _approver_assigned,
    ):
        campaign = _make_pending_campaign(entity, actor, org)
        client = APIClient()
        client.force_authenticate(user=actor)
        response = client.post("/api/v1/workflow/instances/from-campaign/", {
            "campaign_id": campaign.id,
            "activate": True,
        })
        assert response.status_code == http_status.HTTP_201_CREATED
        assert response.data["status"] == InstanceStatus.ACTIVE


# ---------------------------------------------------------------------------
# 3. from-campaign fails with module inactive
# ---------------------------------------------------------------------------

class TestFromCampaignModuleInactive:
    def test_module_inactive_returns_422(self, entity, actor, org, campaign_module_active):
        campaign_module_active.is_active = False
        campaign_module_active.save()
        campaign = _make_pending_campaign(entity, actor, org)
        client = APIClient()
        client.force_authenticate(user=actor)
        response = client.post("/api/v1/workflow/instances/from-campaign/", {
            "campaign_id": campaign.id,
        })
        assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_module_inactive_raises_in_service(self, entity, actor, org, campaign_module_active):
        campaign_module_active.is_active = False
        campaign_module_active.save()
        campaign = _make_pending_campaign(entity, actor, org)
        with pytest.raises(ModuleInactiveError):
            create_campaign_workflow_draft(campaign, started_by=actor)


# ---------------------------------------------------------------------------
# 4. from-campaign fails with no workflow configured
# ---------------------------------------------------------------------------

class TestFromCampaignNoWorkflow:
    def test_no_template_returns_422(self, entity, actor, org, campaign_module_active):
        """Module active but no workflow template → 422."""
        campaign = _make_pending_campaign(entity, actor, org)
        client = APIClient()
        client.force_authenticate(user=actor)
        response = client.post("/api/v1/workflow/instances/from-campaign/", {
            "campaign_id": campaign.id,
        })
        assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_no_template_raises_in_service(self, entity, actor, org, campaign_module_active):
        campaign = _make_pending_campaign(entity, actor, org)
        with pytest.raises(WorkflowNotConfiguredError):
            create_campaign_workflow_draft(campaign, started_by=actor)


# ---------------------------------------------------------------------------
# 5. campaign service helper: wrong status raises CampaignStateError
# ---------------------------------------------------------------------------

class TestCreateCampaignWorkflowDraftStateGuard:
    def test_draft_campaign_raises(self, entity, actor, org):
        campaign = Campaign.objects.create(
            org=org, scope_node=entity, name="Draft", code="d001",
            requested_amount="100.00", created_by=actor,
            status=CampaignStatus.DRAFT,
        )
        with pytest.raises(CampaignStateError):
            create_campaign_workflow_draft(campaign, started_by=actor)


# ---------------------------------------------------------------------------
# 6. Activating campaign workflow sets Campaign.status = in_review
# ---------------------------------------------------------------------------

class TestCampaignStatusSyncOnActivation:
    def test_activate_sets_campaign_in_review(
        self, entity, actor, org, campaign_module_active, single_step_version, _approver_assigned,
    ):
        campaign = _make_pending_campaign(entity, actor, org)
        instance = create_campaign_workflow_draft(campaign, started_by=actor)
        activate_workflow_instance(instance, activated_by=actor)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.IN_REVIEW


# ---------------------------------------------------------------------------
# 7. Approving final step sets Campaign.status = approved
# ---------------------------------------------------------------------------

class TestCampaignStatusSyncOnApproval:
    def test_final_approval_sets_campaign_approved(
        self, entity, actor, org, approver, campaign_module_active, single_step_version, _approver_assigned,
    ):
        campaign = _make_pending_campaign(entity, actor, org)
        instance = create_campaign_workflow_draft(campaign, started_by=actor, activate=True)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.IN_REVIEW

        ist = instance.instance_groups.first().instance_steps.first()
        approve_workflow_step(ist, acted_by=approver)
        campaign.refresh_from_db()
        # After workflow APPROVED, finance handoff is created+sent → finance_pending
        assert campaign.status == CampaignStatus.FINANCE_PENDING


# ---------------------------------------------------------------------------
# 8. Rejecting step sets Campaign.status = rejected
# ---------------------------------------------------------------------------

class TestCampaignStatusSyncOnRejection:
    def test_rejection_sets_campaign_rejected(
        self, entity, actor, org, approver, campaign_module_active, single_step_version, _approver_assigned,
    ):
        campaign = _make_pending_campaign(entity, actor, org)
        instance = create_campaign_workflow_draft(campaign, started_by=actor, activate=True)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.IN_REVIEW

        ist = instance.instance_groups.first().instance_steps.first()
        reject_workflow_step(ist, acted_by=approver, note="Not ready")
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.REJECTED


# ---------------------------------------------------------------------------
# 9. Campaign stays PENDING_WORKFLOW while workflow is DRAFT
# ---------------------------------------------------------------------------

class TestCampaignStatusWhileDraft:
    def test_campaign_pending_workflow_while_instance_draft(
        self, entity, actor, org, campaign_module_active, single_step_version, _approver_assigned,
    ):
        campaign = _make_pending_campaign(entity, actor, org)
        instance = create_campaign_workflow_draft(campaign, started_by=actor)
        assert instance.status == InstanceStatus.DRAFT
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.PENDING_WORKFLOW


# ---------------------------------------------------------------------------
# 10. Invoice workflow behavior unchanged (regression)
# ---------------------------------------------------------------------------

class TestInvoiceWorkflowUnchanged:
    """
    Smoke test: invoice status sync still works after the refactor of
    _sync_invoice_status_on_workflow_change → _sync_subject_status_on_workflow_change.
    """

    @pytest.fixture
    def invoice_setup(self, db, org, entity, actor, approver, approver_role, _approver_assigned):
        from apps.invoices.models import Invoice, InvoiceStatus
        from apps.modules.models import ModuleActivation, ModuleType

        ModuleActivation.objects.create(
            module=ModuleType.INVOICE, scope_node=entity,
            is_active=True, override_parent=True,
        )
        template = WorkflowTemplate.objects.create(
            name="Invoice WF", module="invoice", scope_node=entity, created_by=actor,
        )
        version = WorkflowTemplateVersion.objects.create(
            template=template, version_number=1, status=VersionStatus.PUBLISHED,
        )
        g = StepGroup.objects.create(
            template_version=version, name="Review", display_order=1,
            parallel_mode=ParallelMode.SINGLE, on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=g, name="Step", required_role=approver_role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1, default_user=approver,
        )
        invoice = Invoice.objects.create(
            title="Test Invoice", amount="1000.00", currency="INR",
            scope_node=entity, created_by=actor, status=InvoiceStatus.DRAFT,
        )
        return version, invoice

    def test_invoice_approved_after_workflow_approval(
        self, entity, actor, approver, invoice_setup,
    ):
        from apps.invoices.models import InvoiceStatus
        version, invoice = invoice_setup
        instance = create_workflow_instance_draft(
            template_version=version,
            subject_type="invoice",
            subject_id=invoice.pk,
            subject_scope_node=entity,
            started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.IN_REVIEW

        ist = instance.instance_groups.first().instance_steps.first()
        approve_workflow_step(ist, acted_by=approver)
        invoice.refresh_from_db()
        # After workflow APPROVED, finance handoff is created+sent → finance_pending
        assert invoice.status == InvoiceStatus.FINANCE_PENDING

    def test_invoice_rejected_after_workflow_rejection(
        self, entity, actor, approver, invoice_setup,
    ):
        from apps.invoices.models import InvoiceStatus
        version, invoice = invoice_setup
        instance = create_workflow_instance_draft(
            template_version=version,
            subject_type="invoice",
            subject_id=invoice.pk,
            subject_scope_node=entity,
            started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)
        ist = instance.instance_groups.first().instance_steps.first()
        reject_workflow_step(ist, acted_by=approver)
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.REJECTED
