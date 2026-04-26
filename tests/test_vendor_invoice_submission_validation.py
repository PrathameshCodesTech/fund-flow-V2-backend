import pytest
from decimal import Decimal

from apps.access.models import Role
from apps.core.models import NodeType, Organization, ScopeNode
from apps.invoices.models import (
    Invoice,
    InvoiceStatus,
    VendorInvoiceSubmission,
    VendorInvoiceSubmissionStatus,
)
from apps.invoices.services import (
    SubmissionValidationError,
    _validate_workflow_route_for_submission,
    submit_vendor_invoice_with_route,
    validate_vendor_submission_for_submit,
)
from apps.users.models import User
from apps.vendors.models import Vendor, VendorSubmissionRoute
from apps.workflow.models import (
    ParallelMode,
    RejectionAction,
    ScopeResolutionPolicy,
    StepGroup,
    StepKind,
    VersionStatus,
    WorkflowStep,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Validation Org", code="validation-org")


@pytest.fixture
def scope_node(org):
    return ScopeNode.objects.create(
        org=org,
        parent=None,
        name="Root",
        code="root",
        node_type=NodeType.COMPANY,
        path="/validation-org/root",
        depth=0,
    )


@pytest.fixture
def child_node(org, scope_node):
    return ScopeNode.objects.create(
        org=org,
        parent=scope_node,
        name="Marketing",
        code="marketing",
        node_type=NodeType.DEPARTMENT,
        path="/validation-org/root/marketing",
        depth=1,
    )


@pytest.fixture
def actor(db):
    return User.objects.create_user(email="validation-actor@example.com", password="pass")


@pytest.fixture
def vendor(org, scope_node):
    return Vendor.objects.create(
        org=org,
        scope_node=scope_node,
        vendor_name="Test Vendor",
        email="test@vendor.com",
        sap_vendor_id="SAP-001",
        operational_status="active",
        po_mandate_enabled=False,
    )


@pytest.fixture
def po_vendor(org, scope_node):
    return Vendor.objects.create(
        org=org,
        scope_node=scope_node,
        vendor_name="PO Vendor",
        email="po@vendor.com",
        sap_vendor_id="SAP-PO",
        operational_status="active",
        po_mandate_enabled=True,
    )


@pytest.fixture
def suspended_vendor(org, scope_node):
    return Vendor.objects.create(
        org=org,
        scope_node=scope_node,
        vendor_name="Suspended Vendor",
        email="suspended@vendor.com",
        sap_vendor_id="SAP-SUSP",
        operational_status="suspended",
        po_mandate_enabled=False,
    )


def _create_template(scope_node, actor, *, code="test-workflow", module="invoice", is_active=True, published=True, default_user=None, required_role=None):
    if required_role is None:
        required_role, _ = Role.objects.get_or_create(
            org=scope_node.org,
            code=f"{code}-role",
            defaults={"name": f"{code} Role"},
        )
    template = WorkflowTemplate.objects.create(
        code=code,
        name=code,
        module=module,
        is_active=is_active,
        scope_node=scope_node,
        created_by=actor,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template,
        version_number=1,
        status=VersionStatus.PUBLISHED if published else VersionStatus.DRAFT,
    )
    group = StepGroup.objects.create(
        template_version=version,
        name="Stage 1",
        display_order=0,
        parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    WorkflowStep.objects.create(
        group=group,
        name="Review",
        display_order=0,
        step_kind=StepKind.NORMAL_APPROVAL,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        default_user=default_user,
        required_role=required_role,
    )
    return template


@pytest.fixture
def workflow_template(scope_node, actor):
    return _create_template(scope_node, actor, default_user=actor, code="valid-workflow")


@pytest.fixture
def send_to_route(org, workflow_template):
    return VendorSubmissionRoute.objects.create(
        org=org,
        label="Valid Route",
        code="valid-route",
        workflow_template=workflow_template,
        is_active=True,
    )


@pytest.fixture
def valid_submission(vendor, scope_node):
    return VendorInvoiceSubmission.objects.create(
        vendor=vendor,
        scope_node=scope_node,
        status=VendorInvoiceSubmissionStatus.READY,
        normalized_data={
            "vendor_invoice_number": "INV-001",
            "invoice_date": "2026-04-01",
            "currency": "INR",
            "total_amount": "10000",
        },
    )


class TestFieldValidation:
    def test_suspended_vendor_blocks(self, suspended_vendor, scope_node, send_to_route):
        submission = VendorInvoiceSubmission.objects.create(
            vendor=suspended_vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-001",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "10000",
            },
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert not result.is_valid
        assert "vendor_status" in result.field_errors

    def test_missing_normalized_data_blocks(self, vendor, scope_node, send_to_route):
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={},
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert not result.is_valid
        assert "_normalized_data" in result.field_errors

    @pytest.mark.parametrize(
        ("payload", "field"),
        [
            ({"invoice_date": "2026-04-01", "currency": "INR", "total_amount": "100"}, "vendor_invoice_number"),
            ({"vendor_invoice_number": "INV-1", "currency": "INR", "total_amount": "100"}, "invoice_date"),
            ({"vendor_invoice_number": "INV-1", "invoice_date": "2026-04-01", "total_amount": "100"}, "currency"),
            ({"vendor_invoice_number": "INV-1", "invoice_date": "2026-04-01", "currency": "INR"}, "total_amount"),
        ],
    )
    def test_missing_required_fields_block(self, vendor, scope_node, send_to_route, payload, field):
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data=payload,
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert not result.is_valid
        assert field in result.field_errors

    @pytest.mark.parametrize(
        ("value", "field"),
        [
            ("not-a-date", "invoice_date"),
            ("inr", "currency"),
            ("0", "total_amount"),
            ("-10", "total_amount"),
        ],
    )
    def test_invalid_field_values_block(self, vendor, scope_node, send_to_route, value, field):
        payload = {
            "vendor_invoice_number": "INV-001",
            "invoice_date": "2026-04-01",
            "currency": "INR",
            "total_amount": "10000",
        }
        payload[field] = value
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data=payload,
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert not result.is_valid
        assert field in result.field_errors

    def test_due_date_before_invoice_date_blocks(self, vendor, scope_node, send_to_route):
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-001",
                "invoice_date": "2026-04-10",
                "due_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "10000",
            },
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert not result.is_valid
        assert "due_date" in result.field_errors

    def test_po_mandate_missing_blocks(self, po_vendor, scope_node, send_to_route):
        submission = VendorInvoiceSubmission.objects.create(
            vendor=po_vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-001",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "10000",
            },
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert not result.is_valid
        assert "po_number" in result.field_errors

    def test_subtotal_tax_mismatch_blocks(self, vendor, scope_node, send_to_route):
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-001",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "10000",
                "subtotal_amount": "8000",
                "tax_amount": "1500",
            },
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert not result.is_valid
        assert "total_amount" in result.field_errors


class TestDuplicateValidation:
    def test_duplicate_submission_blocks(self, vendor, scope_node, send_to_route):
        VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-DUP",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "100",
            },
        )
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-DUP",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "100",
            },
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert not result.is_valid
        assert "vendor_invoice_number" in result.field_errors

    def test_duplicate_invoice_blocks(self, vendor, scope_node, send_to_route):
        Invoice.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            title="Existing",
            amount=Decimal("100"),
            currency="INR",
            status=InvoiceStatus.PENDING_WORKFLOW,
            vendor_invoice_number="INV-EXIST",
        )
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-EXIST",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "100",
            },
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert not result.is_valid
        assert "vendor_invoice_number" in result.field_errors

    def test_terminal_records_do_not_block_duplicates(self, vendor, scope_node, send_to_route):
        VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.CANCELLED,
            normalized_data={
                "vendor_invoice_number": "INV-TERM",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "100",
            },
        )
        Invoice.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            title="Paid",
            amount=Decimal("100"),
            currency="INR",
            status=InvoiceStatus.PAID,
            vendor_invoice_number="INV-PAID",
        )
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-TERM",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "100",
            },
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert result.is_valid


class TestRouteValidation:
    def test_inactive_route_blocks(self, valid_submission, org, workflow_template):
        route = VendorSubmissionRoute.objects.create(
            org=org,
            label="Inactive Route",
            code="inactive-route",
            workflow_template=workflow_template,
            is_active=False,
        )
        with pytest.raises(Exception, match="not active"):
            _validate_workflow_route_for_submission(valid_submission, route)

    def test_inactive_template_blocks(self, org, scope_node, actor, valid_submission):
        template = _create_template(scope_node, actor, code="inactive-template", is_active=False, default_user=actor)
        route = VendorSubmissionRoute.objects.create(
            org=org,
            label="Inactive Template",
            code="inactive-template-route",
            workflow_template=template,
            is_active=True,
        )
        with pytest.raises(Exception, match="not active"):
            _validate_workflow_route_for_submission(valid_submission, route)

    def test_wrong_module_blocks(self, org, scope_node, actor, valid_submission):
        template = _create_template(scope_node, actor, code="expense-template", module="expense", default_user=actor)
        route = VendorSubmissionRoute.objects.create(
            org=org,
            label="Expense Route",
            code="expense-route",
            workflow_template=template,
            is_active=True,
        )
        with pytest.raises(Exception, match="not an invoice workflow"):
            _validate_workflow_route_for_submission(valid_submission, route)

    def test_no_published_version_blocks(self, org, scope_node, actor, valid_submission):
        template = _create_template(scope_node, actor, code="draft-template", published=False, default_user=actor)
        route = VendorSubmissionRoute.objects.create(
            org=org,
            label="Draft Route",
            code="draft-route",
            workflow_template=template,
            is_active=True,
        )
        with pytest.raises(Exception, match="no published version"):
            _validate_workflow_route_for_submission(valid_submission, route)

    def test_child_scoped_template_blocks(self, org, scope_node, child_node, actor, vendor):
        template = _create_template(child_node, actor, code="child-template", default_user=actor)
        route = VendorSubmissionRoute.objects.create(
            org=org,
            label="Child Route",
            code="child-route",
            workflow_template=template,
            is_active=True,
        )
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-001",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "10000",
            },
        )
        with pytest.raises(Exception, match="not available for invoices"):
            _validate_workflow_route_for_submission(submission, route)

    def test_no_actionable_steps_blocks(self, org, scope_node, actor, valid_submission):
        template = WorkflowTemplate.objects.create(
            code="no-steps-template",
            name="No Steps",
            module="invoice",
            is_active=True,
            scope_node=scope_node,
            created_by=actor,
        )
        WorkflowTemplateVersion.objects.create(
            template=template,
            version_number=1,
            status=VersionStatus.PUBLISHED,
        )
        route = VendorSubmissionRoute.objects.create(
            org=org,
            label="No Steps Route",
            code="no-steps-route",
            workflow_template=template,
            is_active=True,
        )
        with pytest.raises(Exception, match="no actionable steps"):
            _validate_workflow_route_for_submission(valid_submission, route)

    def test_default_user_step_passes(self, valid_submission, send_to_route):
        published_version, first_step = _validate_workflow_route_for_submission(valid_submission, send_to_route)
        assert published_version is not None
        assert first_step.default_user is not None

    def test_required_role_without_assignee_blocks(self, org, scope_node, actor, valid_submission):
        role = Role.objects.create(org=org, name="Approver", code="approver")
        template = _create_template(
            scope_node,
            actor,
            code="role-template",
            default_user=None,
            required_role=role,
        )
        route = VendorSubmissionRoute.objects.create(
            org=org,
            label="Role Route",
            code="role-route",
            workflow_template=template,
            is_active=True,
        )
        with pytest.raises(Exception, match="eligible approvers"):
            _validate_workflow_route_for_submission(valid_submission, route)


class TestWarnings:
    def test_warnings_are_non_blocking(self, vendor, scope_node, send_to_route):
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "INV-WARN",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "10000",
                "due_date": "2026-09-01",
            },
            confidence_score=Decimal("0.4"),
        )
        result = validate_vendor_submission_for_submit(submission, send_to_route)
        assert result.is_valid
        codes = {warning["code"] for warning in result.warnings}
        assert {"low_confidence", "due_date_far_future", "missing_description"} <= codes


class TestSubmitIntegration:
    def test_invalid_submission_sets_needs_correction(self, vendor, scope_node, send_to_route):
        submission = VendorInvoiceSubmission.objects.create(
            vendor=vendor,
            scope_node=scope_node,
            status=VendorInvoiceSubmissionStatus.READY,
            normalized_data={
                "vendor_invoice_number": "",
                "invoice_date": "2026-04-01",
                "currency": "INR",
                "total_amount": "10000",
            },
        )
        with pytest.raises(SubmissionValidationError) as exc_info:
            submit_vendor_invoice_with_route(submission, None, send_to_route)
        assert not exc_info.value.result.is_valid
        submission.refresh_from_db()
        assert submission.status == VendorInvoiceSubmissionStatus.NEEDS_CORRECTION
        assert len(submission.validation_errors) > 0
