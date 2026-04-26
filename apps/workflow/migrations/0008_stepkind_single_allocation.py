# Generated migration for Phase 1: add SINGLE_ALLOCATION to StepKind and
# SINGLE_ALLOC_SUBMITTED to WorkflowEventType.
# No SQL DDL changes — CharField choices are enforced at the application layer only.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workflow', '0007_add_branch_approval_policy'),
    ]

    operations = [
        migrations.AlterField(
            model_name='workflowstep',
            name='step_kind',
            field=models.CharField(
                choices=[
                    ('NORMAL_APPROVAL', 'Normal Approval'),
                    ('SPLIT_BY_SCOPE', 'Split By Scope'),
                    ('JOIN_BRANCHES', 'Join Branches'),
                    ('RUNTIME_SPLIT_ALLOCATION', 'Runtime Split Allocation'),
                    ('SINGLE_ALLOCATION', 'Single Allocation'),
                ],
                default='NORMAL_APPROVAL',
                help_text='Controls whether this step is a normal approval or a split/join step',
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name='workflowevent',
            name='event_type',
            field=models.CharField(
                choices=[
                    ('STEP_ASSIGNED', 'Step Assigned'),
                    ('STEP_APPROVED', 'Step Approved'),
                    ('STEP_REJECTED', 'Step Rejected'),
                    ('STEP_ORPHANED', 'Step Orphaned'),
                    ('STEP_REASSIGNED', 'Step Reassigned'),
                    ('INSTANCE_STUCK', 'Instance Stuck'),
                    ('INSTANCE_FROZEN', 'Instance Frozen'),
                    ('INSTANCE_APPROVED', 'Instance Approved'),
                    ('INSTANCE_REJECTED', 'Instance Rejected'),
                    ('BRANCH_ASSIGNED', 'Branch Assigned'),
                    ('BRANCH_APPROVED', 'Branch Approved'),
                    ('BRANCH_REJECTED', 'Branch Rejected'),
                    ('BRANCH_REASSIGNED', 'Branch Reassigned'),
                    ('BRANCHES_SPLIT', 'Branches Split'),
                    ('BRANCHES_JOINED', 'Branches Joined'),
                    ('SPLIT_ALLOCATIONS_SUBMITTED', 'Split Allocations Submitted'),
                    ('SPLIT_ALLOCATION_CORRECTED', 'Split Allocation Corrected'),
                    ('SINGLE_ALLOC_SUBMITTED', 'Single Allocation Submitted'),
                    ('ALLOCATION_BUDGET_RESERVED', 'Allocation Budget Reserved'),
                    ('ALLOCATION_BUDGET_RELEASED', 'Allocation Budget Released'),
                    ('ALLOCATION_BUDGET_CONSUMED', 'Allocation Budget Consumed'),
                ],
                max_length=30,
            ),
        ),
    ]
