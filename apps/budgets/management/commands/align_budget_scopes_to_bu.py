from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.budgets.models import Budget
from apps.campaigns.models import Campaign
from apps.core.models import ScopeNode
from apps.manual_expenses.models import ManualExpenseEntry


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _infer_target_code(budget: Budget) -> str | None:
    code = _normalize(budget.code)
    name = _normalize(budget.name)

    for token in ("north", "south", "west", "incity", "corporate"):
        if token in code or token in name:
            return token

    # These remain corporate-level pools in the current Horizon marketing setup.
    if any(token in code or token in name for token in ("park", "branding", "esg", "bd")):
        return "corporate"

    return None


class Command(BaseCommand):
    help = (
        "Reassign Marketing-scoped budgets to child BU scope nodes based on "
        "budget code/name, then align linked campaign/manual-expense scope nodes. "
        "Runs in dry-run mode unless --apply is provided."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--org-code",
            default="horizon",
            help="Organisation code to align (default: horizon).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this flag the command only prints the plan.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        org_code = options["org_code"]
        apply_changes = options["apply"]

        try:
            marketing = ScopeNode.objects.select_related("org").get(
                org__code=org_code,
                code="marketing",
            )
        except ScopeNode.DoesNotExist as exc:
            raise CommandError(f"Marketing scope node not found for org '{org_code}'.") from exc

        child_nodes = {
            _normalize(node.code): node
            for node in ScopeNode.objects.filter(parent=marketing, is_active=True)
        }
        if not child_nodes:
            raise CommandError(
                f"No active child BU nodes found under '{marketing.name}' for org '{org_code}'."
            )

        budgets_to_move: list[tuple[Budget, ScopeNode]] = []
        unresolved: list[Budget] = []
        for budget in Budget.objects.select_related("scope_node").filter(scope_node=marketing).order_by("id"):
            target_code = _infer_target_code(budget)
            target_node = child_nodes.get(target_code or "")
            if not target_node:
                unresolved.append(budget)
                continue
            if budget.scope_node_id != target_node.id:
                budgets_to_move.append((budget, target_node))

        campaign_updates: list[tuple[Campaign, ScopeNode]] = []
        expense_updates: list[tuple[ManualExpenseEntry, ScopeNode]] = []

        moved_budget_ids = {budget.id for budget, _ in budgets_to_move}
        target_scope_by_budget_id = {budget.id: node for budget, node in budgets_to_move}

        linked_campaigns = (
            Campaign.objects.select_related("budget", "scope_node")
            .filter(budget_id__in=moved_budget_ids)
            .order_by("id")
        )
        for campaign in linked_campaigns:
            target_scope = target_scope_by_budget_id[campaign.budget_id]
            if campaign.scope_node_id != target_scope.id:
                campaign_updates.append((campaign, target_scope))

        linked_expenses = (
            ManualExpenseEntry.objects.select_related("budget", "scope_node")
            .filter(budget_id__in=moved_budget_ids)
            .order_by("id")
        )
        for expense in linked_expenses:
            target_scope = target_scope_by_budget_id[expense.budget_id]
            if expense.scope_node_id != target_scope.id:
                expense_updates.append((expense, target_scope))

        self.stdout.write(self.style.NOTICE(f"Org: {marketing.org.name} ({marketing.org.code})"))
        self.stdout.write(self.style.NOTICE(f"Marketing node: {marketing.id} {marketing.name}"))
        self.stdout.write(self.style.NOTICE(f"Budgets to move: {len(budgets_to_move)}"))
        for budget, node in budgets_to_move:
            self.stdout.write(f"  Budget #{budget.id} {budget.code} -> {node.name}")

        if unresolved:
            self.stdout.write(self.style.WARNING(f"Unresolved budgets: {len(unresolved)}"))
            for budget in unresolved:
                self.stdout.write(f"  Budget #{budget.id} {budget.code} ({budget.name})")

        self.stdout.write(self.style.NOTICE(f"Campaign scope updates: {len(campaign_updates)}"))
        for campaign, node in campaign_updates:
            self.stdout.write(f"  Campaign #{campaign.id} {campaign.code} -> {node.name}")

        self.stdout.write(self.style.NOTICE(f"Manual expense scope updates: {len(expense_updates)}"))
        for expense, node in expense_updates:
            self.stdout.write(f"  Expense #{expense.id} -> {node.name}")

        if not apply_changes:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("Dry run only. Re-run with --apply to persist changes."))
            return

        for budget, node in budgets_to_move:
            budget.scope_node = node
            budget.save(update_fields=["scope_node", "updated_at"])

        for campaign, node in campaign_updates:
            campaign.scope_node = node
            campaign.save(update_fields=["scope_node", "updated_at"])

        for expense, node in expense_updates:
            expense.scope_node = node
            expense.save(update_fields=["scope_node", "updated_at"])

        self.stdout.write(self.style.SUCCESS("Budget scope alignment applied successfully."))
