from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Organization, ScopeNode, NodeType
from apps.core.services import build_node_path, get_node_depth


BRANCH_SEED = {
    "north": [
        ("north-farukhnagar-1", "Farukhnagar I"),
        ("north-farukhnagar-2", "Farukhnagar II"),
        ("north-bilaspur", "Bilaspur"),
        ("north-koka", "Koka"),
        ("north-luhari", "Luhari"),
    ],
    "south": [
        ("south-park-1", "South Park 1"),
        ("south-park-2", "South Park 2"),
        ("south-park-3", "South Park 3"),
    ],
    "west": [
        ("west-park-1", "West Park 1"),
        ("west-park-2", "West Park 2"),
        ("west-park-3", "West Park 3"),
    ],
    "incity": [
        ("incity-park-1", "Incity Park 1"),
        ("incity-park-2", "Incity Park 2"),
        ("incity-park-3", "Incity Park 3"),
    ],
}


class Command(BaseCommand):
    help = (
        "Seed branch / park child scope nodes under Horizon Marketing regions "
        "(North, South, West, Incity). Corporate is intentionally excluded."
    )

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(code="horizon")
        except Organization.DoesNotExist as exc:
            raise CommandError("Organization 'horizon' does not exist.") from exc

        try:
            marketing = ScopeNode.objects.get(org=org, code="marketing")
        except ScopeNode.DoesNotExist as exc:
            raise CommandError("Marketing scope node does not exist for Horizon.") from exc

        created = 0
        updated = 0

        for region_code, branch_defs in BRANCH_SEED.items():
            try:
                region = ScopeNode.objects.get(org=org, code=region_code, parent=marketing)
            except ScopeNode.DoesNotExist as exc:
                raise CommandError(f"Region '{region_code}' does not exist under Marketing.") from exc

            for branch_code, branch_name in branch_defs:
                defaults = {
                    "name": branch_name,
                    "node_type": NodeType.BRANCH,
                    "parent": region,
                    "path": build_node_path(region, org, branch_code),
                    "depth": get_node_depth(region),
                    "is_active": True,
                }
                node, was_created = ScopeNode.objects.get_or_create(
                    org=org,
                    code=branch_code,
                    defaults=defaults,
                )
                node.name = branch_name
                node.node_type = NodeType.BRANCH
                node.parent = region
                node.path = build_node_path(region, org, branch_code)
                node.depth = get_node_depth(region)
                node.is_active = True
                node.save(update_fields=["name", "node_type", "parent", "path", "depth", "is_active"])
                if was_created:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Seeded Horizon region branches. created={created} updated={updated}"
        ))
