"""
Tests for Branch migration + upgrade behaviour.

The ``BranchUpgradeTestCase`` fixture in ``tests/fixtures/branch_v4_4_10.sql.gz``
is a whole-DB pg_dump captured on a clean NetBox 4.4.10 install: the source
install's ``public`` schema (with users, content types, seed data, and a
Branch row pointing at the captured branch schema) plus that branch schema
(with its own seed data and ``core_objectchange`` rows).

The upgrade test drops the test DB's ``public`` schema, replays the dump to
re-establish the 4.4.10 state in both schemas, runs ``manage.py migrate`` to
bring ``public`` forward to the current NetBox version, then runs
``MigrateBranchJob`` to bring the branch schema forward, and finally
exercises a merge + revert cycle.

``MigrateBranchSignalTestCase`` covers the regression for GitHub issue #542:
ORM writes inside data migrations must not create ``ObjectChange`` records in
the branch schema, and the signal handlers disconnected during the job must
be reconnected afterwards.
"""
import gzip
import time
import typing
import uuid
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.signals import handle_changed_object, handle_deleted_object
from dcim.models import Manufacturer
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, connections
from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse
from netbox.context_managers import event_tracking
from netbox.signals import post_clean
from utilities.exceptions import AbortTransaction

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.contextvars import active_branch as active_branch_var
from netbox_branching.jobs import MigrateBranchJob
from netbox_branching.models import Branch
from netbox_branching.signal_receivers import validate_branching_operations

User = get_user_model()

FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'branch_v4_4_10.sql.gz'

# Branch name baked into the fixture by dump_branch_fixture.
FIXTURE_BRANCH_NAME = '_fixture_dump'


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


def _drop_schema_contents(cursor, schema):
    """
    Drop every table in a schema, plus the ``natural_sort`` collation
    NetBox installs in ``public``. The schema itself is *not* dropped —
    callers are expected to truncate the contents and reuse the existing
    schema.

    ``DROP SCHEMA "public" CASCADE`` on a full NetBox database needs
    locks on every dependent object at once (331 tables + ~1000 indexes
    + every FK + the ``natural_sort`` collation). Even though the table
    drops above run one statement at a time, Django's
    ``TransactionTestCase`` does not always leave the connection in
    autocommit — locks accumulate across the per-table statements and
    the final ``DROP SCHEMA CASCADE`` tips the shared lock table into
    ``out of shared memory``. Leaving the schema in place sidesteps the
    problem entirely; the only conflict with re-loading the fixture is
    the ``natural_sort`` collation, which we drop explicitly.
    """
    cursor.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = %s",
        [schema],
    )
    for (table,) in cursor.fetchall():
        cursor.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE')

    # Drop any custom collations in this schema. The fixture re-creates
    # ``natural_sort`` in ``public``; without an explicit drop here the
    # second invocation of ``load_whole_db_fixture()`` collides on it.
    cursor.execute(
        """
        SELECT collname FROM pg_collation
         WHERE collnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s)
        """,
        [schema],
    )
    for (coll,) in cursor.fetchall():
        cursor.execute(f'DROP COLLATION IF EXISTS "{schema}"."{coll}" CASCADE')


def _drop_branch_schema(cursor, schema):
    """Drop a single branch schema (small, no shared objects, safe to cascade)."""
    cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _drop_branch_schemas(cursor):
    """Drop every ``branch_*`` schema in the current DB."""
    cursor.execute("""
        SELECT nspname FROM pg_namespace
         WHERE nspname LIKE 'branch_%'
    """)
    for (name,) in cursor.fetchall():
        _drop_branch_schema(cursor, name)


def load_whole_db_fixture():
    """
    Replace the current test DB's ``public`` contents (plus any branch
    schemas) with the contents of the whole-DB fixture, then run
    ``manage.py migrate`` to bring everything from the source NetBox
    version forward to current.

    Used by ``BranchUpgradeTestCase``. The operation is destructive:
    anything Django's test runner staged in public is replaced. Tests
    that load the fixture should not depend on the runner's normal
    serialized rollback state.

    We empty ``public`` rather than dropping and re-creating it, because
    ``DROP SCHEMA "public" CASCADE`` on a full NetBox database exhausts
    PostgreSQL's shared lock table — see ``_drop_schema_contents``.
    """
    with gzip.open(FIXTURE_PATH, 'rt', encoding='utf-8') as f:
        sql = f.read()

    with connection.cursor() as cursor:
        _drop_schema_contents(cursor, 'public')
        _drop_branch_schemas(cursor)
        cursor.execute(sql)
        cursor.execute("SET search_path TO public")

    # The connection's cached schema introspection is now stale (table OIDs
    # and column lists changed when we replaced public's contents). Close
    # it so subsequent queries open a fresh connection.
    connection.close()

    # Bring public forward to the current NetBox migration head.
    call_command('migrate', verbosity=0)


class BranchUpgradeTestCase(TransactionTestCase):
    serialized_rollback = True

    # A handful of objects the branch creates in its seed data. They do
    # not exist in public before the merge, exist after the merge, and are
    # gone again after the revert. Each entry is ``(model_path, lookup_kwargs)``.
    BRANCH_SEED_PROBES: typing.ClassVar = [
        ('dcim.Site', {'slug': 'test-site'}),
        ('tenancy.Tenant', {'slug': 'acme-corp'}),
        ('dcim.Device', {'name': 'Test Device'}),
        ('ipam.Prefix', {'prefix': '10.0.0.0/24'}),
        ('extras.Tag', {'slug': 'fixture-tag-a'}),
    ]

    def setUp(self):
        # Each test starts from the fixture's 4.4.10 state with public migrated
        # forward. This is destructive — public is dropped and re-loaded —
        # so it cannot share setup with sibling test classes via setUpClass.
        load_whole_db_fixture()

    def tearDown(self):
        # Reset context vars so a stale branch doesn't leak into the next test
        active_branch_var.set(None)

        with connection.cursor() as cursor:
            _drop_branch_schemas(cursor)
        for alias in [a for a in connections.databases if a.startswith('schema_')]:
            connections[alias].close()

    def _probe_models(self):
        """Resolve the BRANCH_SEED_PROBES entries to (model, kwargs) tuples."""
        from django.apps import apps
        return [
            (apps.get_model(path), kwargs)
            for path, kwargs in self.BRANCH_SEED_PROBES
        ]

    def _assert_probes_exist(self, *, exist, msg_prefix):
        for model, kwargs in self._probe_models():
            present = model.objects.filter(**kwargs).exists()
            self.assertEqual(
                present, exist,
                msg=(
                    f"{msg_prefix}: expected {model.__name__} matching "
                    f"{kwargs} to {'exist' if exist else 'be absent'} in main, "
                    f"but it {'was missing' if exist else 'was present'}."
                ),
            )

    def test_upgrade_from_v4_4_10(self):
        """
        A branch captured on an older NetBox version must migrate cleanly to
        the current NetBox version (regression for #542 covers the data-
        migration ObjectChange suppression), and the migrated branch must
        merge and revert cleanly against the migrated main schema.
        """
        branch = Branch.objects.get(name=FIXTURE_BRANCH_NAME)
        user = User.objects.get(username='_fixture_admin')

        # Sanity: the branch-seeded objects must not yet exist in main.
        # They live only in the branch schema until the merge replays them.
        self._assert_probes_exist(exist=False, msg_prefix="Pre-merge")

        # After migrate(), the branch is marked PENDING_MIGRATIONS by
        # check_pending_migrations() because public moved forward but the
        # branch schema didn't.
        branch.refresh_from_db()
        self.assertEqual(
            branch.status, BranchStatusChoices.PENDING_MIGRATIONS,
            msg=f"Expected PENDING_MIGRATIONS, got {branch.status!r}"
        )

        # The fixture preserves the ObjectChange records from when the v4.4.10
        # branch was originally in use. Snapshot the count of all branch-
        # schema changes (using get_changes(), which works regardless of
        # status — get_unmerged_changes() returns empty unless status=READY)
        # so we can verify the schema migration itself didn't add to it.
        changes_before = branch.get_changes().count()

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
        changes_after = branch.get_changes().count()
        self.assertEqual(
            changes_after, changes_before,
            msg=(
                f"Data migrations created {changes_after - changes_before} "
                f"spurious ObjectChange record(s) in the branch "
                f"(before={changes_before}, after={changes_after})"
            )
        )

        # Merge: branch-seeded objects should now exist in main.
        branch.merge(user=user, commit=True)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)
        self._assert_probes_exist(exist=True, msg_prefix="Post-merge")

        # Revert: the merge is undone; branch-seeded objects are gone from main.
        branch.revert(user=user, commit=True)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)
        self._assert_probes_exist(exist=False, msg_prefix="Post-revert")


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
        for branch in Branch.objects.all():
            if hasattr(connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _create_and_provision_branch(self, name='Test Branch'):
        branch = Branch(name=name, merge_strategy='squash')
        branch.save(provision=False)
        branch.provision(user=self.user)

        max_wait = 30
        wait_interval = 0.1
        elapsed = 0

        while elapsed < max_wait:
            branch.refresh_from_db()
            if branch.status == BranchStatusChoices.READY:
                break
            time.sleep(wait_interval)
            elapsed += wait_interval
        else:
            raise TimeoutError(
                f"Branch {branch.name} did not become READY within {max_wait} seconds. "
                f"Status: {branch.status}"
            )

        return branch

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
