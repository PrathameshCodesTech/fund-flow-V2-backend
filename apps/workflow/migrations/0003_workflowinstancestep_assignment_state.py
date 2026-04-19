from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0002_remove_workflowinstance_template_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowinstancestep",
            name="assignment_state",
            field=models.CharField(
                choices=[
                    ("ASSIGNED", "Assigned"),
                    ("ASSIGNMENT_REQUIRED", "Assignment Required"),
                    ("NO_ELIGIBLE_USERS", "No Eligible Users"),
                ],
                default="ASSIGNMENT_REQUIRED",
                help_text=(
                    "How this step's assigned_user was resolved at instance creation. "
                    "ASSIGNED = user confirmed; ASSIGNMENT_REQUIRED = multiple candidates, pick manually; "
                    "NO_ELIGIBLE_USERS = no users hold the required role at the resolved scope node."
                ),
                max_length=30,
            ),
        ),
    ]
