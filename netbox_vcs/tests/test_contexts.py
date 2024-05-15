from django.db import connection
from django.test import TestCase

from dcim.models import Site
from netbox_vcs.constants import PRIMARY_SCHEMA
from netbox_vcs.models import Context
from netbox_vcs.todo import get_tables_to_replicate
from .utils import fetchall, fetchone


class ContextTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        sites = (
            Site(name='Site 1', slug='site-1'),
            Site(name='Site 2', slug='site-2'),
            Site(name='Site 3', slug='site-3'),
        )
        Site.objects.bulk_create(sites)

    def test_create_context(self):
        context = Context(name='Context1')
        context.save()

        # Validate the generated raw schema name
        self.assertEqual(
            context.schema_name,
            'context1',
            msg="Context name does not match expected value"
        )

        tables_to_replicate = get_tables_to_replicate()

        with connection.cursor() as cursor:

            # Check that the schema was created in the database
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [context.schema_name]
            )
            row = cursor.fetchone()
            self.assertIsNotNone(row)

            # Check that all expected tables exist in the schema
            cursor.execute(
                "SELECT * FROM information_schema.tables WHERE table_schema=%s",
                [context.schema_name]
            )
            tables_expected = {*tables_to_replicate, 'extras_objectchange'}
            tables_found = {row.table_name for row in fetchall(cursor)}
            self.assertSetEqual(tables_expected, tables_found)

            # Check that object counts match the primary schema for each table
            for table_name in tables_to_replicate:
                cursor.execute(f"SELECT COUNT(id) FROM {PRIMARY_SCHEMA}.{table_name}")
                primary_count = fetchone(cursor).count
                cursor.execute(f"SELECT COUNT(id) FROM {context.schema_name}.{table_name}")
                context_count = fetchone(cursor).count + 1
                self.assertEqual(
                    primary_count,
                    context_count,
                    msg=f"Table {table_name} object count differs from primary schema"
                )

    def test_delete_context(self):
        context = Context(name='Context1')
        context.save()
        context.delete()

        with connection.cursor() as cursor:

            # Check that the schema no longer exists in the database
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [context.schema_name]
            )
            row = fetchone(cursor)
            self.assertIsNone(row)
