"""
Regression test for upgrading an existing branch to a newer NetBox version.

The fixture in ``tests/fixtures/branch_v4_4_10.sql.gz`` is a pg_dump of a branch
schema captured on a clean NetBox 4.4.10 install. It contains a populated
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
        for alias in [a for a in connections.databases if a.startswith('schema_')]:
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
        A branch captured on an older NetBox version must migrate cleanly to
        the current NetBox version. The fixture covers FK, M2M, and MPTT
        relations across DCIM, IPAM, Tenancy, and Extras so that data
        migrations have realistic rows to operate against.
        """
        user, _ = User.objects.get_or_create(username='upgrade_user')

        Branch.objects.filter(name='upgrade-test').delete()
        branch = Branch(name='upgrade-test')
        branch.save(provision=False)
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.READY)
        branch.refresh_from_db()

        self._load_fixture(branch.schema_name)

        # Confirm the fixture loaded with a populated migration history and
        # at least some seed data (both required for the test to be meaningful).
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT COUNT(*) FROM "{branch.schema_name}".django_migrations')
            self.assertGreater(
                cursor.fetchone()[0], 0,
                msg="Fixture django_migrations table is empty"
            )

        # Run all pending migrations against the branch schema.
        branch.migrate(user=user)

        # Migration completed successfully — branch is back to READY and there
        # are no migrations left to apply.
        branch.refresh_from_db()
        self.assertEqual(
            branch.status, BranchStatusChoices.READY,
            msg=f"Branch ended migration in {branch.status!r}, expected READY"
        )
        # Clear cached_property so we re-read the post-migration plan
        if 'pending_migrations' in branch.__dict__:
            del branch.__dict__['pending_migrations']
        self.assertEqual(
            branch.pending_migrations, [],
            msg=f"Migrations remain pending after migrate(): {branch.pending_migrations}"
        )
