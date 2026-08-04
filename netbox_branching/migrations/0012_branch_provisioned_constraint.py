from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0011_pluggable_backend'),
    ]

    operations = [
        # The flag records that a dataset exists, and a dataset is only ever reachable through
        # the identifier the backend assigned to it, so the two cannot disagree. Safe to apply
        # unconditionally here: 0010's backfill only ever set the flag on a branch which
        # already had an identifier (then named schema_id, renamed by 0011).
        migrations.AddConstraint(
            model_name='branch',
            constraint=models.CheckConstraint(
                condition=models.Q(('provisioned', False)) | models.Q(('backend_id__isnull', False)),
                name='netbox_branching_branch_provisioned_requires_backend_id',
            ),
        ),
    ]
