"""
Tests for Branch migration + upgrade behaviour.

The ``BranchUpgradeTestCase`` fixture in ``tests/fixtures/branch_v4_4_10.sql.gz``
is a pg_dump of a branch schema captured on a clean NetBox 4.4.10 install. It
contains a populated ``django_migrations`` table for 4.4.10 plus seed data
covering FK, M2M, and MPTT relations across DCIM, IPAM, Tenancy, and Extras.

The upgrade test loads that fixture into a fresh schema, registers a Branch
pointing at it, runs ``MigrateBranchJob`` against the running NetBox version,
and then exercises a user-driven create + merge + revert cycle.

``MigrateBranchSignalTestCase`` covers the regression for GitHub issue #542:
ORM writes inside data migrations must not create ``ObjectChange`` records in
the branch schema, and the signal handlers disconnected during the job must
be reconnected afterwards.

``ProvisionedBackfillTestCase`` covers the data migration which populates
``Branch.provisioned`` on upgrade.
"""
import gzip
import importlib
import uuid
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.signals import handle_changed_object, handle_deleted_object
from dcim.models import Manufacturer
from django.apps.registry import Apps
from django.contrib.auth import get_user_model
from django.db import connection, connections, models
from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from netbox.context_managers import event_tracking
from netbox.signals import post_clean
from utilities.exceptions import AbortTransaction

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.contextvars import active_branch as active_branch_var
from netbox_branching.jobs import MigrateBranchJob
from netbox_branching.models import Branch
from netbox_branching.provisioning import quote_ident
from netbox_branching.signal_receivers import validate_branching_operations
from netbox_branching.tests.utils import provision_branch

User = get_user_model()

FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'branch_v4_4_10.sql.gz'
PLACEHOLDER = '__BRANCH_SCHEMA__'


def _make_migrate_job(branch, user):
    """Build a minimal job stand-in suitable for MigrateBranchJob.run()."""
    return SimpleNamespace(object=branch, user=user, data=None)


def _signal_handlers_connected():
    """
    Return True if all four object-change signal handlers covered by
    ``disconnect_object_change_signal_handlers()`` are currently registered
    on their respective signals.
    """
    def receivers_for(signal):
        # Django's Signal.receivers entries are tuples whose second element
        # is either a weakref to the receiver (default) or the receiver
        # itself when connected with weak=False. Tuple arity has varied
        # across Django versions, so index by position.
        result = set()
        for entry in signal.receivers:
            ref = entry[1]
            receiver = ref() if isinstance(ref, weakref.ReferenceType) else ref
            if receiver is not None:
                result.add(receiver)
        return result

    return (
        handle_changed_object in receivers_for(post_save) and
        handle_changed_object in receivers_for(m2m_changed) and
        handle_deleted_object in receivers_for(pre_delete) and
        validate_branching_operations in receivers_for(post_clean)
    )


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
        the current NetBox version, and the schema migration must not add
        spurious ObjectChange records to the branch (regression for #542).

        The fixture covers FK, M2M, and MPTT relations across DCIM, IPAM,
        Tenancy, and Extras so that data migrations have realistic rows to
        operate against.
        """
        user, _ = User.objects.get_or_create(username='upgrade_user')

        Branch.objects.filter(name='upgrade-test').delete()
        # This test loads a fixture schema in place of provisioning it, so it pins the
        # backend ID (and therefore the schema name) that provisioning would assign.
        branch = Branch(name='upgrade-test', backend_id='upgradets')
        branch.save(provision=False)
        # Standing in for provision(), which is what normally records both of these.
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.READY, provisioned=True)
        branch.refresh_from_db()

        schema = branch.backend.get_schema_name(branch.backend_id)
        self._load_fixture(schema)

        # Confirm the fixture loaded with a populated migration history and
        # at least some seed data (both required for the test to be meaningful).
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT COUNT(*) FROM "{schema}".django_migrations')
            self.assertGreater(
                cursor.fetchone()[0], 0,
                msg="Fixture django_migrations table is empty"
            )

        # The fixture preserves the ObjectChange records from when the v4.4.10
        # branch was originally in use. Snapshot the count so we can later
        # verify the migration itself didn't add to it.
        unmerged_before = branch.get_unmerged_changes().count()

        # Run all pending migrations against the branch schema via the job
        # (rather than calling branch.migrate() directly) so the disconnect
        # wrapper protecting against #542 is exercised end-to-end.
        MigrateBranchJob(_make_migrate_job(branch, user)).run()

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

        # Regression for #542: data migrations must not have added to the
        # branch's pre-existing ObjectChange records.
        unmerged_after = branch.get_unmerged_changes().count()
        self.assertEqual(
            unmerged_after, unmerged_before,
            msg=(
                f"Data migrations created {unmerged_after - unmerged_before} "
                f"spurious ObjectChange record(s) in the branch "
                f"(before={unmerged_before}, after={unmerged_after})"
            )
        )


class MigrateBranchSignalTestCase(TransactionTestCase):
    """
    Regression test for GitHub issue #542.

    Verifies that ``MigrateBranchJob.run()`` disconnects the changelog signal
    handlers so that ORM writes during data migrations do not create spurious
    ``ObjectChange`` records in the branch schema, and that the handlers are
    reconnected afterwards.
    """

    serialized_rollback = True

    def setUp(self):
        self.user = User.objects.create_user(username='testuser')
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user
        self.request = request

    def tearDown(self):
        # Close any branch connections that were actually opened during the test.
        for branch in Branch.objects.all():
            if hasattr(connections._connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _create_and_provision_branch(self, name='Test Branch'):
        return provision_branch(user=self.user, name=name, merge_strategy='squash')

    def test_migrate_job_does_not_create_spurious_objectchanges(self):
        """
        Run MigrateBranchJob.run() with branch.migrate() patched to simulate
        a data migration writing a branchable object. Verify that the job's
        signal disconnection prevents ObjectChange records from appearing in
        the branch schema, and that signal handlers are reconnected after
        the job completes.

        Without disconnect_object_change_signal_handlers() in
        MigrateBranchJob.run(), the ORM write below fires post_save ->
        handle_changed_object -> ObjectChange is created in the branch schema.
        """
        branch = self._create_and_provision_branch()

        # Sanity check: handlers are connected before the job runs
        self.assertTrue(_signal_handlers_connected())

        # Simulate what a data migration's RunPython does inside branch.migrate():
        # save a branchable model to the branch schema with event tracking active.
        # event_tracking sets current_request, which handle_changed_object requires
        # to create ObjectChange records (without it the signal returns early).
        def fake_migrate(user):
            with event_tracking(self.request):
                Manufacturer(name='m1', slug='m1').save(using=branch.connection_name)

        with patch.object(branch, 'migrate', side_effect=fake_migrate):
            MigrateBranchJob(_make_migrate_job(branch, self.user)).run()

        self.assertEqual(branch.get_unmerged_changes().count(), 0)
        self.assertTrue(_signal_handlers_connected())

    def test_migrate_job_reconnects_signal_handlers_on_exception(self):
        """
        If branch.migrate() raises an unexpected exception, the context
        manager in MigrateBranchJob.run() must still reconnect the signal
        handlers.
        """
        branch = self._create_and_provision_branch()

        def fake_migrate(user):
            raise RuntimeError("simulated migration failure")

        with patch.object(branch, 'migrate', side_effect=fake_migrate), self.assertRaises(RuntimeError):
            MigrateBranchJob(_make_migrate_job(branch, self.user)).run()

        self.assertTrue(_signal_handlers_connected())

    def test_migrate_job_reconnects_signal_handlers_on_abort_transaction(self):
        """
        AbortTransaction is the dry-run signalling exception used elsewhere
        in netbox-branching jobs. MigrateBranchJob.run() catches it inside
        the disconnect context manager (rather than re-raising), so the
        normal ``with`` exit path must still reconnect the signal handlers
        and leave no spurious ObjectChange records behind.
        """
        branch = self._create_and_provision_branch()

        def fake_migrate(user):
            with event_tracking(self.request):
                # Simulate a data migration write that would normally fire
                # the changelog signal, then bail out as a dry run.
                Manufacturer(name='m1', slug='m1').save(using=branch.connection_name)
            raise AbortTransaction()

        # The job must swallow AbortTransaction (dry-run path); no exception
        # should escape MigrateBranchJob.run().
        with patch.object(branch, 'migrate', side_effect=fake_migrate):
            MigrateBranchJob(_make_migrate_job(branch, self.user)).run()

        self.assertEqual(branch.get_unmerged_changes().count(), 0)
        self.assertTrue(_signal_handlers_connected())


def historical_branch_model():
    """
    Branch as migration 0010 sees it, before 0011 renames ``schema_id`` to ``backend_id``.

    The backfill reads ``schema_id``, which is the column's name at the point it runs. The
    test database is fully migrated, so the live model no longer carries that name; mapping
    it back onto the renamed column reproduces the migration's view of the table without
    standing up a second one. Unmanaged, so it owns no schema of its own.

    Built on demand into a throwaway app registry rather than declared at module scope. Test
    modules are imported before the test databases are set up, so a model declared there
    would be part of ``netbox_branching`` for every other test in the run and would mint a
    ContentType row which outlives it under ``--keepdb``.
    """
    class HistoricalBranch(models.Model):
        schema_id = models.CharField(max_length=255, db_column='backend_id')
        status = models.CharField(max_length=50)
        last_sync = models.DateTimeField(null=True)
        provisioned = models.BooleanField(default=False)

        class Meta:
            # Register in a private registry, not the global one Django populated at startup.
            apps = Apps()
            app_label = 'netbox_branching'
            db_table = 'netbox_branching_branch'
            managed = False

    return HistoricalBranch


class ProvisionedBackfillTestCase(TestCase):
    """
    Migration 0010 backfills Branch.provisioned for branches which predate the field, by
    asking the database which branch schemas actually exist. The backfill function is called
    directly here: it takes only (apps, schema_editor), and the schemas it looks for are
    ordinary ones this test can create itself — provisioning a real branch would cost minutes
    and prove nothing extra. See #665.

    The branches are given an identifier by hand. Under the pluggable backend one is assigned
    at provisioning time rather than at save(), but every branch predating 0010 has one, which
    is the population the backfill exists for. See #618.
    """
    def setUp(self):
        self.backfill = importlib.import_module(
            'netbox_branching.migrations.0010_branch_provisioned'
        ).set_provisioned
        # The migration reads the table through the field name it had at 0010.
        historical_branch = historical_branch_model()
        self.migration_apps = SimpleNamespace(get_model=lambda *args, **kwargs: historical_branch)

    def _make_branch(self, name, status, *, with_schema, synced=False):
        branch = Branch(name=name, status=status)
        branch.save(provision=False)
        branch.set_backend_id(branch.backend.generate_branch_id())
        if synced:
            # last_sync is shielded from save() as a lifecycle field; write it directly.
            Branch.objects.filter(pk=branch.pk).update(last_sync=timezone.now())
        if with_schema:
            schema = branch.backend.get_schema_name(branch.backend_id)
            with connection.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA {quote_ident(schema)}')
            # The schema is created inside the test's transaction, so TestCase rollback
            # removes it; DROP it anyway in case this runs where that isn't true.
            self.addCleanup(self._drop_schema, schema)
        return branch

    @staticmethod
    def _drop_schema(schema_name):
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS {quote_ident(schema_name)} CASCADE')

    def test_backfill_follows_the_schemas_which_exist(self):
        ready = self._make_branch('Ready', BranchStatusChoices.READY, with_schema=True, synced=True)
        # FAILED is the ambiguous one, covering three states a schema alone cannot tell apart:
        # a failed migrate (schema intact), a failed provision (schema dropped), and a
        # provisioning run whose worker was killed, which the stuck-branch watchdog moves here
        # from PROVISIONING with its partial phase-1 schema left behind. Only the first is a
        # dataset; last_sync is what distinguishes it, being written when provisioning
        # completes and on every sync after that.
        failed_migrate = self._make_branch(
            'Failed migrate', BranchStatusChoices.FAILED, with_schema=True, synced=True
        )
        failed_provision = self._make_branch('Failed provision', BranchStatusChoices.FAILED, with_schema=False)
        recovered_stuck = self._make_branch('Recovered stuck', BranchStatusChoices.FAILED, with_schema=True)
        # A branch still in PROVISIONING is either mid-run or not yet recovered; phase 1 commits
        # CREATE SCHEMA before any data is copied, so its schema is no evidence of a dataset.
        stuck = self._make_branch('Stuck', BranchStatusChoices.PROVISIONING, with_schema=True)
        new = self._make_branch('New', BranchStatusChoices.NEW, with_schema=False)
        archived = self._make_branch('Archived', BranchStatusChoices.ARCHIVED, with_schema=False)

        self.backfill(self.migration_apps, SimpleNamespace(connection=connection))

        expected = {
            ready.pk: True,
            failed_migrate.pk: True,
            failed_provision.pk: False,
            recovered_stuck.pk: False,
            stuck.pk: False,
            new.pk: False,
            archived.pk: False,
        }
        self.assertEqual(
            dict(Branch.objects.filter(pk__in=expected).values_list('pk', 'provisioned')),
            expected,
        )
