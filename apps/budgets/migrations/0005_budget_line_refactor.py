"""
Restructure Budget into a named header model and introduce BudgetLine.
"""
from decimal import Decimal
import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('budgets', '0004_alter_budgetrule_options'),
    ]

    operations = [
        # ── 1. Add name + code to Budget ─────────────────────────────────────
        migrations.AddField(
            model_name='budget',
            name='name',
            field=models.CharField(
                default='',
                help_text='Human-readable name, e.g. FY27 Marketing - North',
                max_length=255,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='budget',
            name='code',
            field=models.CharField(
                default='',
                help_text='Short code, e.g. FY27-MKT-NORTH',
                max_length=100,
            ),
            preserve_default=False,
        ),

        # ── 2. Drop old unique constraint on Budget ───────────────────────────
        migrations.RemoveConstraint(
            model_name='budget',
            name='unique_budget_allocation',
        ),

        # ── 3. Remove category+status index (references category field) ───────
        migrations.RemoveIndex(
            model_name='budget',
            name='budgets_categor_ef1a4e_idx',
        ),

        # ── 4. Remove category + subcategory FKs from Budget ─────────────────
        migrations.RemoveField(model_name='budget', name='category'),
        migrations.RemoveField(model_name='budget', name='subcategory'),

        # ── 5. Add new unique constraint on Budget ────────────────────────────
        migrations.AddConstraint(
            model_name='budget',
            constraint=models.UniqueConstraint(
                fields=['scope_node', 'financial_year', 'code'],
                name='unique_budget_per_scope_code_year',
            ),
        ),

        # ── 6. Create BudgetLine ──────────────────────────────────────────────
        migrations.CreateModel(
            name='BudgetLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('allocated_amount', models.DecimalField(
                    decimal_places=2,
                    default=Decimal('0'),
                    max_digits=14,
                    validators=[django.core.validators.MinValueValidator(Decimal('0'))],
                )),
                ('reserved_amount', models.DecimalField(
                    decimal_places=2,
                    default=Decimal('0'),
                    max_digits=14,
                    validators=[django.core.validators.MinValueValidator(Decimal('0'))],
                )),
                ('consumed_amount', models.DecimalField(
                    decimal_places=2,
                    default=Decimal('0'),
                    max_digits=14,
                    validators=[django.core.validators.MinValueValidator(Decimal('0'))],
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('budget', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='lines',
                    to='budgets.budget',
                )),
                ('category', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='budget_lines',
                    to='budgets.budgetcategory',
                )),
                ('subcategory', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='budget_lines',
                    to='budgets.budgetsubcategory',
                )),
            ],
            options={
                'db_table': 'budget_lines',
            },
        ),
        migrations.AddIndex(
            model_name='budgetline',
            index=models.Index(fields=['budget', 'category'], name='budget_line_budget__04018f_idx'),
        ),

        # ── 7. Add budget_line FK to BudgetConsumption ───────────────────────
        migrations.AddField(
            model_name='budgetconsumption',
            name='budget_line',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='consumptions',
                to='budgets.budgetline',
            ),
        ),
        migrations.AddIndex(
            model_name='budgetconsumption',
            index=models.Index(
                fields=['budget_line', 'source_type', 'source_id'],
                name='budget_cons_budget__8c0ac0_idx',
            ),
        ),

        # ── 8. Add budget_line FK to BudgetVarianceRequest ───────────────────
        migrations.AddField(
            model_name='budgetvariancerequest',
            name='budget_line',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='variance_requests',
                to='budgets.budgetline',
            ),
        ),
        migrations.AddIndex(
            model_name='budgetvariancerequest',
            index=models.Index(
                fields=['budget_line', 'status'],
                name='budget_vari_budget__205dc2_idx',
            ),
        ),
    ]
