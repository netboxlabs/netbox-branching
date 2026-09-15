from django.db import migrations, models
from netbox.plugins import get_plugin_config

# Branch statuses which never have a dataset behind them, quoted rather than imported so
# that a later edit to the choice set cannot change what this migration did.
UNPROVISIONED_STATUSES = ('new', 'archived')


def set_provisioned(apps, schema_editor):
    """
    Backfill Branch.provisioned from the datasets which actually exist.

    Every branch predating this field was provisioned by the schema backend — it is the
    only one which existed — so "does a dataset exist" is answerable exactly: look for the
    branch's schema. That beats inferring it from status, which cannot distinguish a
    branch whose provision failed part-way (identifier committed, schema dropped) from one
    whose later migrate failed (schema fully intact).
    """
    Branch = apps.get_model('netbox_branching', 'Branch')
    schema_prefix = get_plugin_config('netbox_branching', 'schema_prefix')

    # pg_namespace rather than information_schema.schemata: the latter lists only schemas
    # the current role owns or holds a privilege on, and a branch schema created by another
    # role would be silently read as missing.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT nspname FROM pg_catalog.pg_namespace")
        existing_schemas = {row[0] for row in cursor.fetchall()}

    candidates = Branch.objects.exclude(backend_id=None).exclude(status__in=UNPROVISIONED_STATUSES)
    provisioned = [
        branch.pk for branch in candidates.only('pk', 'backend_id')
        if f'{schema_prefix}{branch.backend_id}' in existing_schemas
    ]
    if provisioned:
        Branch.objects.filter(pk__in=provisioned).update(provisioned=True)


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0010_pluggable_backend'),
    ]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='provisioned',
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.RunPython(set_provisioned, migrations.RunPython.noop),
        # Applied after the backfill, which only ever sets the flag on a branch that
        # already has an identifier.
        migrations.AddConstraint(
            model_name='branch',
            constraint=models.CheckConstraint(
                condition=models.Q(('provisioned', False)) | models.Q(('backend_id__isnull', False)),
                name='netbox_branching_branch_provisioned_requires_backend_id',
            ),
        ),
    ]
