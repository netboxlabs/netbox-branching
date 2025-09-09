from django.db import connection, migrations

from netbox.plugins import get_plugin_config
from netbox_branching.choices import BranchStatusChoices


def copy_table(apps, schema_editor):
    """
    Create a copy of the extras_tag_object_types table in each active branch.
    """
    Branch = apps.get_model('netbox_branching', 'Branch')

    table = 'extras_tag_object_types'
    schema_prefix = get_plugin_config('netbox_branching', 'schema_prefix')

    with connection.cursor() as cursor:
        main_table = f'public.{table}'

        for branch in Branch.objects.filter(status=BranchStatusChoices.READY):
            print(f'\n    Copying {table} for branch {branch.name} ({branch.schema_id})... ', end='')
            schema_name = f'{schema_prefix}{branch.schema_id}'
            schema_table = f'{schema_name}.{table}'

            # Abort if the table already exists (somehow)
            cursor.execute(
                f"SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='{schema_name}' AND tablename='{table}')"
            )
            if cursor.fetchone()[0]:
                print('Skipping; table already exists.', end='')
                continue

            # Copy the extras_tag_object_types table to the branch schema
            cursor.execute(f"CREATE TABLE {schema_table} ( LIKE {main_table} INCLUDING INDEXES )")

            # Copy table data
            cursor.execute(f"INSERT INTO {schema_table} SELECT * FROM {main_table}")

            # Set the default value for the ID column to the sequence associated with the source table
            cursor.execute(
                f"ALTER TABLE {schema_table} ALTER COLUMN id SET DEFAULT nextval('extras_tag_object_types_id_seq')"
            )

            # Rename indexes
            cursor.execute(
                f"ALTER INDEX {schema_name}.extras_tag_object_types_objecttype_id_idx "
                f"RENAME TO extras_tag_object_types_contenttype_id_c1b220c3"
            )
            cursor.execute(
                f"ALTER INDEX {schema_name}.extras_tag_object_types_tag_id_idx "
                f"RENAME TO extras_tag_object_types_tag_id_2e1aab29"
            )
            cursor.execute(
                f"ALTER INDEX {schema_name}.extras_tag_object_types_tag_id_objecttype_id_key "
                f"RENAME TO extras_tag_object_types_tag_id_contenttype_id_2ff9910c_uniq"
            )

            print('Success.', end='')

    print('\n ', end='')  # Padding for final "OK"


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0005_branch_applied_migrations'),
    ]

    operations = [
        migrations.RunPython(
            code=copy_table,
            reverse_code=migrations.RunPython.noop
        ),
    ]
