import re

from django.db import migrations, models


def generate_template_codes(apps, schema_editor):
    """
    Populate code for all existing WorkflowTemplate rows and mark each as
    the default (they were the sole template per module+scope before this migration).

    Code is derived by slugifying the name; appends the row id on collision.
    is_default=True because each was the only template for its (module, scope_node).
    """
    WorkflowTemplate = apps.get_model("workflow", "WorkflowTemplate")

    used = {}  # (module, scope_node_id) -> set[str]
    for template in WorkflowTemplate.objects.all().order_by("id"):
        key = (template.module, template.scope_node_id)
        if key not in used:
            used[key] = set()

        # Simple slug: lowercase alphanumerics, spaces/special chars → hyphens
        base = re.sub(r"[^\w\s-]", "", template.name.lower()).strip()
        base = re.sub(r"[\s_-]+", "-", base)[:90]
        if not base:
            base = "template"

        code = base
        if code in used[key]:
            code = f"{base}-{template.id}"
        used[key].add(code)

        template.code = code
        template.is_default = True
        template.save(update_fields=["code", "is_default"])


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0005_workflowstep_allocation_total_policy_and_more"),
    ]

    operations = [
        # 1. Add new fields (code blank initially for data migration)
        migrations.AddField(
            model_name="workflowtemplate",
            name="code",
            field=models.SlugField(max_length=100, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="workflowtemplate",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="workflowtemplate",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="workflowtemplate",
            name="is_default",
            field=models.BooleanField(default=False),
        ),
        # 2. Populate code + is_default for all existing rows
        migrations.RunPython(generate_template_codes, migrations.RunPython.noop),
        # 3. Remove old single-template-per-node constraint
        migrations.RemoveConstraint(
            model_name="workflowtemplate",
            name="unique_template_per_module_per_node",
        ),
        # 4. Add new constraints: unique code per module+scope, unique default per module+scope
        migrations.AddConstraint(
            model_name="workflowtemplate",
            constraint=models.UniqueConstraint(
                fields=["module", "scope_node", "code"],
                name="unique_template_code_per_module_per_node",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowtemplate",
            constraint=models.UniqueConstraint(
                fields=["module", "scope_node"],
                condition=models.Q(is_default=True),
                name="unique_default_per_module_per_node",
            ),
        ),
        # 5. Drop the temporary AddField default now that all rows have a code value
        migrations.AlterField(
            model_name="workflowtemplate",
            name="code",
            field=models.SlugField(
                blank=True,
                max_length=100,
                help_text="Stable slug identifier, unique per module+scope_node. Auto-generated from name if blank.",
            ),
        ),
    ]
