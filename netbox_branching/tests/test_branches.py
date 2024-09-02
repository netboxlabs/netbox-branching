import re

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TransactionTestCase, override_settings

from netbox_branching.constants import MAIN_SCHEMA
from netbox_branching.models import Branch
from netbox_branching.utilities import get_tables_to_replicate
from .utils import fetchall, fetchone


class BranchTestCase(TransactionTestCase):
    serialized_rollback = True

    def test_create_branch(self):
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)

        tables_to_replicate = get_tables_to_replicate()

        with connection.cursor() as cursor:

            # Check that the schema was created in the database
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [branch.schema_name]
            )
            row = cursor.fetchone()
            self.assertIsNotNone(row)

            # Check that all expected tables exist in the schema
            cursor.execute(
                "SELECT * FROM information_schema.tables WHERE table_schema=%s",
                [branch.schema_name]
            )
            tables_expected = {*tables_to_replicate, 'core_objectchange'}
            tables_found = {row.table_name for row in fetchall(cursor)}
            self.assertSetEqual(tables_expected, tables_found)

            # Check that object counts match the main schema for each table
            for table_name in tables_to_replicate:
                cursor.execute(f"SELECT COUNT(id) FROM {MAIN_SCHEMA}.{table_name}")
                main_count = fetchone(cursor).count
                cursor.execute(f"SELECT COUNT(id) FROM {branch.schema_name}.{table_name}")
                branch_count = fetchone(cursor).count
                self.assertEqual(
                    main_count,
                    branch_count,
                    msg=f"Table {table_name} object count differs from main schema"
                )

    def test_delete_branch(self):
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)
        branch.delete()

        with connection.cursor() as cursor:

            # Check that the schema no longer exists in the database
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [branch.schema_name]
            )
            row = fetchone(cursor)
            self.assertIsNone(row)

    def test_branch_schema_id(self):
        branch = Branch(name='Branch 1')
        self.assertIsNotNone(branch.schema_id, msg="Schema ID has not been set")
        self.assertIsNotNone(re.match(r'^[a-z0-9]{8}', branch.schema_id), msg="Schema ID does not conform")
        schema_id = branch.schema_id

        branch.save(provision=False)
        branch.refresh_from_db()
        self.assertEqual(branch.schema_id, schema_id, msg="Schema ID was changed during save()")

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'max_branches': 2,
        }
    })
    def text_max_branches(self):
        """
        Verify that the max_branches config parameter is enforced.
        """
        Branch.objects.bulk_create((
            Branch(name='Branch 1'),
            Branch(name='Branch 2'),
        ))

        branch = Branch(name='Branch 3')
        with self.assertRaises(ValidationError):
            branch.full_clean()
