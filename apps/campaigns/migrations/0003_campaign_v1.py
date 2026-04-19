from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('campaigns', '0002_initial'),
        ('budgets', '0003_budgetcategory_budgetconsumption_budgetrule_and_more'),
        ('core', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── 1. Remove old index (scope_node, status) - will re-add below ───────
        migrations.RemoveIndex(
            model_name='campaign',
            name='campaigns_scope_n_30bd68_idx',
        ),

        # ── 2. Remove old fields ──────────────────────────────────────────────
        migrations.RemoveField(
            model_name='campaign',
            name='title',
        ),
        migrations.RemoveField(
            model_name='campaign',
            name='budget',
        ),

        # ── 3. Update status choices ──────────────────────────────────────────
        migrations.AlterField(
            model_name='campaign',
            name='status',
            field=models.CharField(
                choices=[
                    ('draft', 'Draft'),
                    ('pending_budget', 'Pending Budget'),
                    ('budget_variance_pending', 'Budget Variance Pending'),
                    ('pending_workflow', 'Pending Workflow'),
                    ('in_review', 'In Review'),
                    ('approved', 'Approved'),
                    ('rejected', 'Rejected'),
                    ('cancelled', 'Cancelled'),
                ],
                default='draft',
                max_length=30,
            ),
        ),

        # ── 4. Add new Campaign fields ────────────────────────────────────────
        migrations.AddField(
            model_name='campaign',
            name='org',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='campaigns',
                to='core.organization',
            ),
        ),
        migrations.AddField(
            model_name='campaign',
            name='name',
            field=models.CharField(default='', max_length=255),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='campaign',
            name='code',
            field=models.CharField(default='', max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='campaign',
            name='description',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='campaign',
            name='campaign_type',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='campaign',
            name='start_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='campaign',
            name='end_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='campaign',
            name='requested_amount',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0'),
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(Decimal('0.01'))],
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='campaign',
            name='approved_amount',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0'),
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(Decimal('0'))],
            ),
        ),
        migrations.AddField(
            model_name='campaign',
            name='currency',
            field=models.CharField(default='INR', max_length=10),
        ),
        migrations.AddField(
            model_name='campaign',
            name='category',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='campaigns',
                to='budgets.budgetcategory',
            ),
        ),
        migrations.AddField(
            model_name='campaign',
            name='subcategory',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='campaigns',
                to='budgets.budgetsubcategory',
            ),
        ),
        migrations.AddField(
            model_name='campaign',
            name='budget',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='campaigns',
                to='budgets.budget',
            ),
        ),
        migrations.AddField(
            model_name='campaign',
            name='budget_variance_request',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='campaigns',
                to='budgets.budgetvariancerequest',
            ),
        ),

        # ── 5. Update model options (ordering) ────────────────────────────────
        migrations.AlterModelOptions(
            name='campaign',
            options={'ordering': ['-created_at']},
        ),

        # ── 6. Create CampaignDocument model ─────────────────────────────────
        migrations.CreateModel(
            name='CampaignDocument',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=255)),
                ('file_url', models.CharField(max_length=500)),
                ('document_type', models.CharField(blank=True, max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('campaign', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='documents',
                    to='campaigns.campaign',
                )),
                ('uploaded_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='campaign_documents',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'db_table': 'campaign_documents',
                'ordering': ['-created_at'],
            },
        ),

        # ── 7. New indexes on Campaign ────────────────────────────────────────
        migrations.AddIndex(
            model_name='campaign',
            index=models.Index(fields=['org', 'status'], name='campaigns_org_id_status_idx'),
        ),
        migrations.AddIndex(
            model_name='campaign',
            index=models.Index(fields=['scope_node', 'status'], name='campaigns_scope_status_idx'),
        ),
        migrations.AddIndex(
            model_name='campaign',
            index=models.Index(fields=['category'], name='campaigns_category_idx'),
        ),
        migrations.AddIndex(
            model_name='campaign',
            index=models.Index(fields=['budget'], name='campaigns_budget_idx'),
        ),
    ]
