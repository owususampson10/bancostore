# Generalizes Task 13's Binary-Bonus-only audit trail (BinaryBonusCycleRun/
# Failure) into a job_name-discriminated CommissionCycleRun/Failure, once
# Task 14's Matching Bonus needed the exact same guarantee (2026-07-22).
# Existing rows (there are none in any real deployment yet -- nothing is
# live-deployed per SPEC.md) are backfilled with job_name="calculate-binary-
# bonus", matching apps/commissions/tasks.py's BINARY_BONUS_TASK_NAME, since
# every row this model has ever written was in fact a Binary Bonus cycle.

from django.db import migrations, models


def backfill_job_name(apps, schema_editor):
    CommissionCycleRun = apps.get_model("commissions", "CommissionCycleRun")
    CommissionCycleRun.objects.filter(job_name="").update(
        job_name="calculate-binary-bonus"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("commissions", "0002_initial"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="BinaryBonusCycleRun",
            new_name="CommissionCycleRun",
        ),
        migrations.RenameModel(
            old_name="BinaryBonusCycleFailure",
            new_name="CommissionCycleFailure",
        ),
        migrations.AddField(
            model_name="commissioncyclerun",
            name="job_name",
            field=models.CharField(default="", max_length=50),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_job_name, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="commissioncyclerun",
            name="run_at",
            field=models.DateTimeField(),
        ),
        migrations.AddConstraint(
            model_name="commissioncyclerun",
            constraint=models.UniqueConstraint(
                fields=("job_name", "run_at"), name="unique_job_name_run_at"
            ),
        ),
    ]
