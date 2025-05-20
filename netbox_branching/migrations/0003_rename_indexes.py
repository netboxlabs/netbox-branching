from django.db import connection, migrations

from netbox.plugins import get_plugin_config
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.utilities import get_sql_results

# Indexes to ignore as they are removed in a NetBox v4.3 migration
SKIP = (
    'dcim_cabletermination_termination_type_id_termination_id_idx',     # Removed in dcim.0207_remove_redundant_indexes
    'vpn_l2vpntermination_assigned_object_type_id_assigned_objec_idx',  # Removed in vpn.0009_remove_redundant_indexes
    'vpn_tunneltermination_termination_type_id_termination_id_idx',     # Removed in vpn.0009_remove_redundant_indexes
)


def rename_indexes(apps, schema_editor):
    """
    Rename all indexes within each branch to match the main schema.
    """
    Branch = apps.get_model('netbox_branching', 'Branch')
    schema_prefix = get_plugin_config('netbox_branching', 'schema_prefix')
    main_schema = get_plugin_config('netbox_branching', 'main_schema')

    with connection.cursor() as cursor:

        for branch in Branch.objects.filter(status=BranchStatusChoices.READY):
            print(f'\n    Renaming indexes for branch {branch.name} ({branch.schema_id})...', end='')
            schema_name = f'{schema_prefix}{branch.schema_id}'

            # Fetch all SQL indexes from the branch schema
            cursor.execute(
                f"SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = '{schema_name}'"
            )
            branch_indexes = get_sql_results(cursor)

            for branch_index in branch_indexes:

                # Skip index if applicable
                if branch_index.indexname in SKIP:
                    continue

                # Find the matching index in main based on its table & definition
                definition = branch_index.indexdef.split(' USING ', maxsplit=1)[1]
                cursor.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname=%s AND tablename=%s AND indexdef LIKE %s",
                    [main_schema, branch_index.tablename, f'% {definition}']
                )
                if result := cursor.fetchone():
                    new_name = result[0]
                    if new_name != branch_index.indexname:
                        sql = f"ALTER INDEX {schema_name}.{branch_index.indexname} RENAME TO {new_name}"
                        try:
                            cursor.execute(sql)
                        except Exception as e:
                            print(sql)
                            raise e

    print('\n ', end='')  # Padding for final "OK"


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0002_branch_schema_id_unique'),
    ]

    operations = [
        migrations.RunPython(
            code=rename_indexes,
            reverse_code=migrations.RunPython.noop
        ),
    ]
