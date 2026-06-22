from decimal import Decimal

from django.test import TestCase

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
