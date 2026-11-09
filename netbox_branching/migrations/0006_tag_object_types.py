from django.db import connection, migrations
from netbox.plugins import get_plugin_config

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.utilities import get_sql_results


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
                "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname=%s AND tablename=%s)",
                [schema_name, table]
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

            # Rename indexes to match main, matching on definition since their names follow the column name
            cursor.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s",
                [schema_name, table]
            )
            for index in get_sql_results(cursor):
                definition = index.indexdef.split(' USING ', maxsplit=1)[1]
                unique = index.indexdef.startswith('CREATE UNIQUE ')
                cursor.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename=%s "
                    "AND split_part(indexdef, ' USING ', 2)=%s AND starts_with(indexdef, 'CREATE UNIQUE ')=%s",
                    [table, definition, unique]
                )
                if (result := cursor.fetchone()) and result[0] != index.indexname:
                    cursor.execute(f"ALTER INDEX {schema_name}.{index.indexname} RENAME TO {result[0]}")

            # If main has applied extras.0131 but the branch has not, restore the column name the branch expects
            cursor.execute(
                f"SELECT EXISTS (SELECT 1 FROM {schema_name}.django_migrations "
                f"WHERE app='extras' AND name='0131_concrete_objecttype')"
            )
            branch_has_0131 = cursor.fetchone()[0]
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s AND column_name='contenttype_id')",
                [schema_name, table]
            )
            if cursor.fetchone()[0] and not branch_has_0131:
                cursor.execute(f"ALTER TABLE {schema_table} RENAME COLUMN contenttype_id TO objecttype_id")

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
