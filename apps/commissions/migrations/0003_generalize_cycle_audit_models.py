# Generalizes Task 13's Binary-Bonus-only audit trail (BinaryBonusCycleRun/
# Failure) into a job_name-discriminated CommissionCycleRun/Failure, once
# Task 14's Matching Bonus needed the exact same guarantee (2026-07-22).
# Existing rows (there are none in any real deployment yet -- nothing is
# live-deployed per SPEC.md) are backfilled with job_name="calculate-binary-
# bonus", matching apps/commissions/tasks.py's BINARY_BONUS_TASK_NAME, since
# every row this model has ever written was in fact a Binary Bonus cycle.
#
# Reversal note (CodeRabbit review, 2026-07-22): the AlterField step below
# drops run_at's standalone unique=True in favor of the (job_name, run_at)
# composite constraint added after it. Reversing this migration re-adds
# unique=True to run_at alone, which would fail (loudly, with a normal DB
# IntegrityError -- not silently) if a Binary Bonus row and a Matching
# Bonus row ever shared the exact same microsecond-precision run_at by
# then. Left as Django's default auto-reverse rather than engineered
# around: the collision this guards against requires two independent
# timezone.now() calls, from jobs on a 10-minute and a 7-day cadence
# respectively, to land on the identical microsecond, and a failed
# reversal is a safe failure mode (the migrate-down simply stops and
# reports why) rather than a data-corrupting one.

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
