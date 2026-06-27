import json
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from apps.access.models import (
    Permission,
    PermissionAction,
    PermissionResource,
    Role,
    RolePermission,
    UserRoleAssignment,
)
from apps.audit.models import AuditLog
from apps.budgets.models import (
    Budget,
    BudgetCategory,
    BudgetConsumption,
    BudgetLine,
    BudgetStatus,
    BudgetSubCategory,
    ConsumptionType,
)
from apps.budgets.selectors import get_budget_line_live_balances
from apps.core.models import NodeType, Organization, ScopeNode
from apps.invoices.models import (
    Invoice,
    InvoiceAllocation,
    InvoiceAllocationSource,
    InvoiceDocument,
    InvoiceEntrySource,
    InvoiceStatus,
)
from apps.users.models import User
from apps.vendors.models import OperationalStatus, Vendor


class HistoricalInvoicePostingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test Org", code="test-org")
        self.marketing = ScopeNode.objects.create(
            org=self.org,
            name="Marketing",
            code="marketing",
            node_type=NodeType.DEPARTMENT,
            path="/test-org/marketing",
            depth=0,
        )
        self.region = ScopeNode.objects.create(
            org=self.org,
            parent=self.marketing,
            name="North",
            code="north",
            node_type=NodeType.REGION,
            path="/test-org/marketing/north",
            depth=1,
        )
        self.branch = ScopeNode.objects.create(
            org=self.org,
            parent=self.region,
            name="Farukhnagar I",
            code="farukhnagar-i",
            node_type=NodeType.BRANCH,
            path="/test-org/marketing/north/farukhnagar-i",
            depth=2,
        )
        self.admin = User.objects.create_user(email="admin@example.com", password="password")
        self.hod = User.objects.create_user(email="hod@example.com", password="password")
        admin_role = Role.objects.create(org=self.org, name="Tenant Admin", code="tenant_admin")
        hod_role = Role.objects.create(org=self.org, name="HOD", code="hod")
        historical_permission, _ = Permission.objects.get_or_create(
            action=PermissionAction.HISTORICAL_POST,
            resource=PermissionResource.INVOICE,
        )
        read_permission, _ = Permission.objects.get_or_create(
            action=PermissionAction.READ,
            resource=PermissionResource.INVOICE,
        )
        RolePermission.objects.create(role=admin_role, permission=historical_permission)
        RolePermission.objects.create(role=admin_role, permission=read_permission)
        RolePermission.objects.create(role=hod_role, permission=read_permission)
        UserRoleAssignment.objects.create(user=self.admin, role=admin_role, scope_node=self.marketing)
        UserRoleAssignment.objects.create(user=self.hod, role=hod_role, scope_node=self.marketing)

        self.vendor = Vendor.objects.create(
            org=self.org,
            scope_node=self.marketing,
            vendor_name="Existing Vendor",
            email="vendor@example.com",
            sap_vendor_id="SAP-1001",
            operational_status=OperationalStatus.ACTIVE,
        )
        self.category = BudgetCategory.objects.create(
            org=self.org,
            name="Events",
            code="EVENTS",
        )
        self.subcategory = BudgetSubCategory.objects.create(
            category=self.category,
            name="Customer Event",
            code="CUSTOMER-EVENT",
        )
        self.second_category = BudgetCategory.objects.create(
            org=self.org,
            name="Print",
            code="PRINT",
        )
        self.budget = Budget.objects.create(
            org=self.org,
            scope_node=self.branch,
            name="FY27 North - Farukhnagar I",
            code="FY27-NORTH-FI",
            financial_year="2026-27",
            allocated_amount=Decimal("1000.00"),
            currency="INR",
            status=BudgetStatus.ACTIVE,
        )
        self.line = BudgetLine.objects.create(
            budget=self.budget,
            category=self.category,
            subcategory=self.subcategory,
            allocated_amount=Decimal("600.00"),
        )
        self.second_line = BudgetLine.objects.create(
            budget=self.budget,
            category=self.second_category,
            subcategory=None,
            allocated_amount=Decimal("400.00"),
        )
        self.client = APIClient()

    def payload(self, *, amount="500.00", invoice_number="INV-HIST-001", allocations=None):
        if allocations is None:
            allocations = [{
                "entity": self.branch.id,
                "budget": self.budget.id,
                "category": self.category.id,
                "subcategory": self.subcategory.id,
                "amount": amount,
            }]
        return {
            "vendor": self.vendor.id,
            "invoice_number": invoice_number,
            "po_number": "",
            "finance_reference_number": "SAP-DOC-001",
            "invoice_date": "2026-06-01",
            "amount": amount,
            "currency": "INR",
            "posting_reason": "Opening historical balance",
            "allocations": allocations,
        }

    def test_preview_returns_impact_without_writing(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/v1/invoices/historical/preview/",
            self.payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["vendor"]["email"], self.vendor.email)
        self.assertEqual(response.data["allocations"][0]["available_before"], "600.00")
        self.assertEqual(response.data["allocations"][0]["available_after"], "100.00")
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(BudgetConsumption.objects.count(), 0)

    def test_options_reuse_region_branch_budget_line_shape(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(
            f"/api/v1/invoices/historical/options/?vendor={self.vendor.id}"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["vendor"]["email"], self.vendor.email)
        north = response.data["allowed_entities"][0]
        self.assertEqual(north["entity_name"], "North")
        self.assertEqual(len(north["child_entities"]), 1)
        branch = north["child_entities"][0]
        self.assertEqual(branch["entity_name"], "Farukhnagar I")
        self.assertEqual(branch["budgets"][0]["id"], self.budget.id)
        self.assertEqual(len(branch["budget_lines"]), 2)
        self.assertFalse(response.data["rules"]["document_required"])

    def test_non_admin_role_cannot_preview_or_post(self):
        self.client.force_authenticate(self.hod)
        preview = self.client.post(
            "/api/v1/invoices/historical/preview/",
            self.payload(),
            format="json",
        )
        posted = self.client.post(
            "/api/v1/invoices/historical/post/",
            self.payload(),
            format="json",
        )

        self.assertEqual(preview.status_code, 403)
        self.assertEqual(posted.status_code, 403)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_multi_line_post_creates_consumed_ledger_without_workflow(self):
        self.client.force_authenticate(self.admin)
        payload = self.payload(
            amount="1000.00",
            allocations=[
                {
                    "entity": self.branch.id,
                    "budget": self.budget.id,
                    "category": self.category.id,
                    "subcategory": self.subcategory.id,
                    "amount": "600.00",
                },
                {
                    "entity": self.branch.id,
                    "budget": self.budget.id,
                    "category": self.second_category.id,
                    "subcategory": None,
                    "amount": "400.00",
                },
            ],
        )
        response = self.client.post(
            "/api/v1/invoices/historical/post/",
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.entry_source, InvoiceEntrySource.HISTORICAL_IMPORT)
        self.assertEqual(invoice.status, InvoiceStatus.HISTORICAL_POSTED)
        self.assertEqual(invoice.finance_reference_number, "SAP-DOC-001")
        self.assertIsNone(invoice.selected_workflow_version_id)
        allocations = InvoiceAllocation.objects.filter(invoice=invoice)
        self.assertEqual(allocations.count(), 2)
        self.assertFalse(allocations.exclude(allocation_source=InvoiceAllocationSource.HISTORICAL_IMPORT).exists())
        self.assertFalse(allocations.exclude(workflow_instance=None, split_step=None).exists())
        self.assertEqual(
            BudgetConsumption.objects.filter(consumption_type=ConsumptionType.CONSUMED).count(),
            2,
        )
        self.budget.refresh_from_db()
        self.assertEqual(self.budget.reserved_amount, Decimal("0.00"))
        self.assertEqual(self.budget.consumed_amount, Decimal("1000.00"))
        self.assertTrue(
            AuditLog.objects.filter(
                action="historical_invoice_posted",
                resource_id=invoice.id,
            ).exists()
        )

    def test_duplicate_vendor_invoice_number_is_rejected(self):
        self.client.force_authenticate(self.admin)
        first = self.client.post(
            "/api/v1/invoices/historical/post/",
            self.payload(),
            format="json",
        )
        second = self.client.post(
            "/api/v1/invoices/historical/post/",
            self.payload(),
            format="json",
        )

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_allocation_total_must_equal_invoice_amount(self):
        self.client.force_authenticate(self.admin)
        payload = self.payload(amount="500.00")
        payload["allocations"][0]["amount"] = "400.00"
        response = self.client.post(
            "/api/v1/invoices/historical/post/",
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("allocations", response.data)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_insufficient_budget_rolls_back_everything(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/v1/invoices/historical/post/",
            self.payload(amount="700.00"),
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(InvoiceAllocation.objects.count(), 0)
        self.assertEqual(BudgetConsumption.objects.count(), 0)

    def test_optional_document_can_be_attached(self):
        self.client.force_authenticate(self.admin)
        payload = self.payload(invoice_number="INV-HIST-DOC")
        payload["allocations"] = json.dumps(payload["allocations"])
        payload["document"] = SimpleUploadedFile(
            "invoice.pdf",
            b"%PDF-1.4 test invoice",
            content_type="application/pdf",
        )
        response = self.client.post(
            "/api/v1/invoices/historical/post/",
            payload,
            format="multipart",
        )

        self.assertEqual(response.status_code, 201, response.data)
        document = InvoiceDocument.objects.get()
        self.assertIsNone(document.submission_id)
        self.assertEqual(document.invoice_id, Invoice.objects.get().id)

    def test_reversal_restores_available_budget_and_keeps_ledger_history(self):
        self.client.force_authenticate(self.admin)
        posted = self.client.post(
            "/api/v1/invoices/historical/post/",
            self.payload(),
            format="json",
        )
        self.assertEqual(posted.status_code, 201, posted.data)
        invoice = Invoice.objects.get()

        reversed_response = self.client.post(
            f"/api/v1/invoices/{invoice.id}/historical/reverse/",
            {"reason": "Incorrect historical mapping"},
            format="json",
        )

        self.assertEqual(reversed_response.status_code, 200, reversed_response.data)
        invoice.refresh_from_db()
        self.line.refresh_from_db()
        self.budget.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.HISTORICAL_REVERSED)
        self.assertEqual(self.line.consumed_amount, Decimal("0.00"))
        self.assertEqual(self.budget.consumed_amount, Decimal("0.00"))
        self.assertEqual(
            BudgetConsumption.objects.filter(consumption_type=ConsumptionType.ADJUSTED).count(),
            1,
        )
        live = get_budget_line_live_balances(self.line)
        self.assertEqual(live["consumed_amount"], Decimal("0.00"))
        self.assertEqual(live["available_amount"], Decimal("600.00"))
        self.assertTrue(
            AuditLog.objects.filter(
                action="historical_invoice_reversed",
                resource_id=invoice.id,
            ).exists()
        )

    def test_historical_invoice_cannot_be_edited_or_deleted(self):
        self.client.force_authenticate(self.admin)
        posted = self.client.post(
            "/api/v1/invoices/historical/post/",
            self.payload(),
            format="json",
        )
        self.assertEqual(posted.status_code, 201, posted.data)
        invoice = Invoice.objects.get()

        edited = self.client.patch(
            f"/api/v1/invoices/{invoice.id}/",
            {"amount": "1.00"},
            format="json",
        )
        deleted = self.client.delete(f"/api/v1/invoices/{invoice.id}/")

        self.assertEqual(edited.status_code, 400)
        self.assertEqual(deleted.status_code, 400)
        self.assertTrue(Invoice.objects.filter(pk=invoice.id).exists())
