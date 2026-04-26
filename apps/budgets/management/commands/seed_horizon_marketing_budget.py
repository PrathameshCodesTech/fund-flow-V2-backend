"""
Management command: seed_horizon_marketing_budget

Seeds the Horizon FY27 Marketing Budget structure idempotently.

Usage:
    python manage.py seed_horizon_marketing_budget

The command is idempotent — safe to rerun. It will not duplicate
categories, subcategories, budgets, or scope nodes.

Options:
    --dry-run   Print what would be created without writing to the DB.
"""
from django.core.management.base import BaseCommand

from apps.budgets.seed_horizon import (
    print_seed_summary,
    seed_horizon_marketing_budget,
)


class Command(BaseCommand):
    help = "Seed Horizon FY27 Marketing Budget structure (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print summary without writing to the database.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry-run mode — no DB writes will occur."))
            self.stdout.write("The following records would be created or updated:")
            self.stdout.write("")
            # Import and print map without running
            from apps.budgets.seed_horizon import (
                _BUDGET_DEFS,
                _CATEGORY_MAP,
                _PARK_BRANDING_EXTRAS,
                _SCOPE_DEFS,
            )
            from apps.budgets.models import BudgetCategory, BudgetSubCategory
            from apps.core.models import Organization, ScopeNode

            org_exists = Organization.objects.filter(code="horizon").exists()
            self.stdout.write(f"  Organization 'Horizon' : {'EXISTS' if org_exists else 'MISSING (would create)'}")

            existing_scopes = set(ScopeNode.objects.filter(org__code="horizon").values_list("code", flat=True))
            for code, name in _SCOPE_DEFS:
                status = "EXISTS" if code in existing_scopes else "MISSING (would create)"
                self.stdout.write(f"  Scope node {code} ({name}): {status}")

            existing_cats = set(BudgetCategory.objects.filter(org__code="horizon").values_list("code", flat=True))
            for key, spec in _CATEGORY_MAP.items():
                status = "EXISTS" if spec["category_code"] in existing_cats else "MISSING (would create)"
                self.stdout.write(f"  Category {spec['category_code']} ({spec['category_name']}): {status}")

            self.stdout.write("")
            self.stdout.write(f"  Budgets (8): {', '.join(sorted(_BUDGET_DEFS.keys()))}")
            self.stdout.write(f"  Park Branding extra subcategories: {len(_PARK_BRANDING_EXTRAS)}")
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Dry-run complete. No changes written."))
            return

        counters = seed_horizon_marketing_budget()
        print_seed_summary(counters, stdout=self.stdout)