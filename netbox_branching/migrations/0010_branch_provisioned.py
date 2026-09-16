from django.db import migrations, models
from netbox.plugins import get_plugin_config

# This migration's only model operation targets Branch, which is exempt from branching, so
# the default heuristic already fakes it on branch schemas. State it explicitly: the backfill
# below reads pg_namespace and must never run with a branch schema first on the search_path.
fake_on_branch = True

# Branch statuses whose schema, where one exists, proves nothing. "new" and "archived" have no
# schema at all. "provisioning" may have one, but phase 1 commits its CREATE SCHEMA before any
# data is copied, so such a branch is either mid-provision or was abandoned there by a dead
# worker; either way there is no dataset to read yet. Nothing is lost by skipping a live
# provision, since provision() sets the flag itself on success.
SKIPPED_STATUSES = ('new', 'archived', 'provisioning')

# "failed" is the status this field exists to disambiguate, and a schema alone cannot do it. A
# failed migrate leaves the schema fully intact; a failed provision drops it. But a provision
# whose worker was killed leaves its partial phase-1 schema behind, and the stuck-branch watchdog
# moves that branch out of "provisioning" and into "failed" within the hour without dropping
# anything (see BranchStatusChoices.RECOVERY_STATUS), so it cannot be skipped by status alone.
# For these, require the mark of a provision which ran to completion: last_sync, written by the
# final update in provision() and by every sync thereafter.
#
# Branches provisioned before v0.5.6 did not get last_sync set automatically, so one which has
# also never synced and has since failed will be recorded here as unprovisioned. That is the
# error worth making: it withholds a dataset which is in fact intact, rather than handing out
# one which is half-built.
AMBIGUOUS_STATUS = 'failed'


def set_provisioned(apps, schema_editor):
    """
    Backfill Branch.provisioned from the schemas which actually exist.

    "Does a dataset exist" is answerable exactly — look for the branch's schema — and that
    beats inferring it from status, which cannot distinguish a branch whose provision failed
    part-way (row committed, schema dropped) from one whose later migrate failed (schema
    fully intact). Both are FAILED. Where a schema is not evidence enough on its own, see
    SKIPPED_STATUSES and AMBIGUOUS_STATUS above.
    """
    Branch = apps.get_model('netbox_branching', 'Branch')
    schema_prefix = get_plugin_config('netbox_branching', 'schema_prefix')

    # Query the database being migrated rather than whichever one the router would pick: the
    # schemas are read from this connection, so the rows compared against them must come from
    # it too, and the result must be written back to the same place.
    db = schema_editor.connection.alias

    # pg_namespace rather than information_schema.schemata: the latter lists only schemas the
    # current role owns or holds a privilege on, and a branch schema created by another role
    # would be silently read as missing.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT nspname FROM pg_catalog.pg_namespace")
        existing_schemas = {row[0] for row in cursor.fetchall()}

    candidates = Branch.objects.using(db).exclude(
        status__in=SKIPPED_STATUSES
    ).exclude(
        status=AMBIGUOUS_STATUS, last_sync__isnull=True
    )
    provisioned = [
        branch.pk for branch in candidates.only('pk', 'schema_id')
        if f'{schema_prefix}{branch.schema_id}' in existing_schemas
    ]
    if provisioned:
        Branch.objects.using(db).filter(pk__in=provisioned).update(provisioned=True)


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0009_changediff_last_updated_auto_now'),
    ]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='provisioned',
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.RunPython(set_provisioned, migrations.RunPython.noop),
    ]
