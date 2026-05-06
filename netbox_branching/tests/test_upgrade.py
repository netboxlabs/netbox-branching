"""
Regression test for upgrading an existing branch to a newer NetBox version.

The fixture in ``tests/fixtures/branch_v4_4_10.sql.gz`` is a pg_dump of a branch
schema captured on a clean NetBox 4.4.10 install (see the
``dump_branch_fixture`` management command). It contains a populated
``django_migrations`` table for 4.4.10 plus seed data covering FK, M2M, and
MPTT relations across DCIM, IPAM, Tenancy, and Extras.

This test loads that fixture into a fresh schema, registers a Branch pointing
at it, and runs ``Branch.migrate()`` against the running NetBox version. With
the routing fix in place, all pending NetBox migrations (including
``dcim.0222_port_mappings``, whose RunPython data migration reads
``FrontPortTemplate.rear_port_position``) must complete cleanly.

Without the fix, the RunPython falls through to the main schema, which has
already been migrated past that column, raising
``ProgrammingError: column ... rear_port_position does not exist``.
"""
import gzip
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.contextvars import active_branch as active_branch_var
from netbox_branching.models import Branch

User = get_user_model()

FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'branch_v4_4_10.sql.gz'
PLACEHOLDER = '__BRANCH_SCHEMA__'


class BranchUpgradeTestCase(TransactionTestCase):
    serialized_rollback = True

    def tearDown(self):
        # Reset context vars so a stale branch doesn't leak into the next test
        active_branch_var.set(None)

        # Drop the branch schema we created (TransactionTestCase doesn't track
        # schemas it didn't make) and close any branch connections so the test
        # database can be torn down cleanly.
        schema = getattr(self, '_loaded_schema', None)
        if schema:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for alias in list(vars(connections._connections)):
            if alias.startswith('schema_'):
                connections[alias].close()

    def _load_fixture(self, schema_name):
        """Create the schema and replay the gzipped SQL fixture into it."""
        with gzip.open(FIXTURE_PATH, 'rt', encoding='utf-8') as f:
            sql = f.read().replace(PLACEHOLDER, schema_name)
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema_name}"')
            cursor.execute(sql)
            # pg_dump's preamble emits `set_config('search_path', '', false)`,
            # which clears the connection's search_path. Reset it so subsequent
            # ORM queries against the default schema work.
            cursor.execute("SET search_path TO public")
        self._loaded_schema = schema_name

    def test_upgrade_from_v4_4_10(self):
        """
        Regression test for the original ``rear_port_position`` bug: when a
        branch captured on 4.4.10 is migrated against current NetBox,
        ``dcim.0222_port_mappings``'s RunPython data migration must read
        ``FrontPortTemplate`` rows from the branch schema, not from main.

        Without the fix, the BranchAwareRouter sends those queries to the main
        schema (which has already been migrated past ``rear_port_position``),
        producing ``ProgrammingError: column ... does not exist``.

        This test asserts:
          1. 0222 completes cleanly and populates the new mapping tables.
          2. If a *later* migration fails (e.g. issue #423 with rename
             collisions), the failure is not the original routing bug.
        """
        user = User.objects.create_user(username='upgrade_user')

        branch = Branch(name='upgrade-test')
        branch.save(provision=False)
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.READY)
        branch.refresh_from_db()

        self._load_fixture(branch.schema_name)

        # Sanity-check the fixture loaded as expected
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT COUNT(*) FROM "{branch.schema_name}".django_migrations')
            self.assertGreater(
                cursor.fetchone()[0], 700,
                msg="Fixture django_migrations table appears empty"
            )
            cursor.execute(f'SELECT COUNT(*) FROM "{branch.schema_name}".dcim_frontporttemplate')
            self.assertEqual(
                cursor.fetchone()[0], 1,
                msg="FrontPortTemplate seed row missing from fixture"
            )

        # Run the migration. The fix routes data-migration ORM queries to the
        # branch schema. Later migrations in the plan may fail for unrelated
        # reasons (see issue #423); the assertions below validate that 0222
        # specifically succeeded and that the failure (if any) is not the
        # original routing bug.
        try:
            branch.migrate(user=user)
        except Exception as e:
            self.assertNotIn(
                'rear_port_position', str(e),
                msg="Migration failed with the original BranchAwareRouter "
                    "routing bug — data migrations are still being sent to "
                    "the main schema instead of the branch."
            )

        # 0222 creates dcim_porttemplatemapping and dcim_portmapping and
        # populates them via RunPython. If the routing fix worked, both
        # tables exist in the branch schema with the expected row counts.
        # The seed data has 1 FrontPortTemplate (→ 1 PortTemplateMapping) and
        # 1 device with 1 auto-created FrontPort (→ 1 PortMapping).
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name='dcim_porttemplatemapping'",
                [branch.schema_name],
            )
            self.assertIsNotNone(
                cursor.fetchone(),
                msg="dcim.0222_port_mappings did not run on the branch — "
                    "dcim_porttemplatemapping table is missing."
            )
            cursor.execute(
                f'SELECT COUNT(*) FROM "{branch.schema_name}".dcim_porttemplatemapping'
            )
            self.assertEqual(
                cursor.fetchone()[0], 1,
                msg="dcim.0222_port_mappings RunPython did not populate "
                    "PortTemplateMapping in the branch schema."
            )
            cursor.execute(
                f'SELECT COUNT(*) FROM "{branch.schema_name}".dcim_portmapping'
            )
            self.assertEqual(
                cursor.fetchone()[0], 1,
                msg="dcim.0222_port_mappings RunPython did not populate "
                    "PortMapping in the branch schema."
            )
