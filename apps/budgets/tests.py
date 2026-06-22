from decimal import Decimal
from io import BytesIO

from django.test import TestCase
from rest_framework.test import APIClient

from apps.access.models import Role, UserRoleAssignment
from apps.budgets.models import (
    Budget,
    BudgetCategory,
    BudgetLine,
    BudgetRevisionLineChangeType,
    BudgetRevisionSource,
    BudgetRevisionStatus,
    BudgetSubCategory,
)
from apps.budgets.services import (
    BudgetRevisionValidationError,
    create_budget_revision,
    parse_scoped_budget_revision_file,
    publish_budget_revision,
)
from apps.core.models import NodeType, Organization, ScopeNode
from apps.users.models import User


class BudgetRevisionServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test Org", code="test-org")
        self.scope = ScopeNode.objects.create(
            org=self.org,
            parent=None,
            name="Marketing",
            code="marketing",
            node_type=NodeType.DEPARTMENT,
            path="/test-org/marketing",
            depth=0,
        )
        self.user = User.objects.create_user(email="hod@example.com", password="password")
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
        self.budget = Budget.objects.create(
            org=self.org,
            scope_node=self.scope,
            name="FY27 Marketing",
            code="FY27-MKT-TEST",
            financial_year="2026-27",
            allocated_amount=Decimal("1000.00"),
            status="active",
        )
        self.line = BudgetLine.objects.create(
            budget=self.budget,
            category=self.category,
            subcategory=self.subcategory,
            allocated_amount=Decimal("1000.00"),
        )

    def test_publish_revision_updates_live_budget_and_keeps_history(self):
        revision = create_budget_revision(
            budget=self.budget,
            proposed_lines=[{
                "category": self.category,
                "subcategory": self.subcategory,
                "allocated_amount": Decimal("1250.00"),
            }],
            source=BudgetRevisionSource.MANUAL,
            change_reason="Increase event allocation",
            created_by=self.user,
        )

        self.assertEqual(revision.status, BudgetRevisionStatus.VALIDATED)
        self.assertEqual(revision.revision_number, 1)
        self.assertEqual(revision.lines.get().change_type, BudgetRevisionLineChangeType.UPDATED)
        self.assertEqual(revision.before_snapshot["allocated_amount"], "1000.00")
        self.assertEqual(revision.after_snapshot["allocated_amount"], "1250.00")

        publish_budget_revision(revision=revision, published_by=self.user)

        self.budget.refresh_from_db()
        self.line.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(self.budget.allocated_amount, Decimal("1250.00"))
        self.assertEqual(self.line.allocated_amount, Decimal("1250.00"))
        self.assertEqual(revision.status, BudgetRevisionStatus.PUBLISHED)
        self.assertEqual(revision.published_by, self.user)

    def test_revision_cannot_reduce_below_committed_usage(self):
        self.line.reserved_amount = Decimal("200.00")
        self.line.consumed_amount = Decimal("500.00")
        self.line.save(update_fields=["reserved_amount", "consumed_amount", "updated_at"])

        with self.assertRaises(BudgetRevisionValidationError):
            create_budget_revision(
                budget=self.budget,
                proposed_lines=[{
                    "category": self.category,
                    "subcategory": self.subcategory,
                    "allocated_amount": Decimal("600.00"),
                }],
                source=BudgetRevisionSource.MANUAL,
                change_reason="Invalid decrease",
                created_by=self.user,
            )

    def test_logical_line_removal_keeps_budget_line_history_row(self):
        revision = create_budget_revision(
            budget=self.budget,
            proposed_lines=[{
                "category": self.category,
                "subcategory": self.subcategory,
                "allocated_amount": Decimal("0.00"),
            }],
            source=BudgetRevisionSource.MANUAL,
            change_reason="Retire unused line",
            created_by=self.user,
        )

        publish_budget_revision(revision=revision, published_by=self.user)

        self.line.refresh_from_db()
        self.budget.refresh_from_db()
        self.assertEqual(self.line.allocated_amount, Decimal("0.00"))
        self.assertEqual(self.budget.allocated_amount, Decimal("0.00"))
        self.assertTrue(BudgetLine.objects.filter(pk=self.line.pk).exists())

    def test_revision_excel_parser_accepts_template_required_markers(self):
        import openpyxl

        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.append([
            "Category Code *",
            "Category Name",
            "Subcategory Code",
            "Subcategory Name",
            "Current Allocation",
            "New Allocation *",
        ])
        worksheet.append(["EVENTS", "Events", "CUSTOMER-EVENT", "Customer Event", 1000, 1250])

        file_obj = BytesIO()
        workbook.save(file_obj)
        file_obj.seek(0)

        self.assertEqual(parse_scoped_budget_revision_file(file_obj), [{
            "category_code": "EVENTS",
            "category name": "Events",
            "subcategory_code": "CUSTOMER-EVENT",
            "subcategory name": "Customer Event",
            "current allocation": "1000",
            "allocated_amount": "1250",
            "row_number": 2,
        }])


class BudgetImportAccessTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Import Org", code="import-org")
        self.scope = ScopeNode.objects.create(
            org=self.org,
            parent=None,
            name="Marketing",
            code="marketing",
            node_type=NodeType.DEPARTMENT,
            path="/import-org/marketing",
            depth=0,
        )
        self.tenant_admin_role = Role.objects.create(
            org=self.org,
            name="Tenant Admin",
            code="tenant_admin",
        )
        self.hod_role = Role.objects.create(
            org=self.org,
            name="HOD",
            code="hod",
        )
        self.tenant_admin = User.objects.create_user(email="admin@example.com", password="password")
        self.hod = User.objects.create_user(email="hod-import@example.com", password="password")
        UserRoleAssignment.objects.create(user=self.tenant_admin, role=self.tenant_admin_role, scope_node=self.scope)
        UserRoleAssignment.objects.create(user=self.hod, role=self.hod_role, scope_node=self.scope)
        self.client = APIClient()

    def test_only_tenant_admin_can_access_bulk_import_endpoints(self):
        self.client.force_authenticate(self.hod)
        denied = self.client.get("/api/v1/budgets/import-batches/")
        self.assertEqual(denied.status_code, 403)

        self.client.force_authenticate(self.tenant_admin)
        allowed = self.client.get("/api/v1/budgets/import-batches/")
        self.assertEqual(allowed.status_code, 200)
