import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from extras.validators import CustomValidator
from netbox.plugins import get_plugin_config
from utilities.exceptions import AbortRequest

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import SKIP_INDEXES
from netbox_branching.forms import BranchForm
from netbox_branching.models import Branch
from netbox_branching.provisioning import quote_ident
from netbox_branching.signals import post_deprovision, pre_deprovision
from netbox_branching.utilities import BranchActionIndicator, get_tables_to_replicate

from .utils import fetchall, fetchone


class BranchTestCase(TransactionTestCase):
    serialized_rollback = True

    def test_create_branch(self):
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)

        main_schema = get_plugin_config('netbox_branching', 'main_schema')
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
            tables_expected = {*tables_to_replicate, 'core_objectchange', 'django_migrations'}
            tables_found = {row.table_name for row in fetchall(cursor)}
            self.assertSetEqual(tables_expected, tables_found)

            # Check that all indexes were renamed to match the main schema
            cursor.execute(
                "SELECT idx_a.schemaname, idx_a.tablename, idx_a.indexname "
                "FROM pg_indexes idx_a "
                "WHERE idx_a.schemaname=%s "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM pg_indexes idx_b "
                "    WHERE idx_b.schemaname=%s AND idx_b.indexname=idx_a.indexname"
                ") ORDER BY idx_a.indexname",
                [branch.schema_name, main_schema]
            )
            # Omit skipped indexes
            # TODO: Remove in v0.6.0
            found_indexes = [
                idx for idx in fetchall(cursor) if idx.indexname not in SKIP_INDEXES
            ]
            self.assertListEqual(found_indexes, [], "Found indexes with unique names in branch schema.")

            # Check that object counts match the main schema for each table
            for table_name in tables_to_replicate:
                cursor.execute(f"SELECT COUNT(id) FROM {main_schema}.{table_name}")
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
            'max_working_branches': 2,
        }
    })
    def test_max_working_branches(self):
        """
        Verify that the max_working_branches config parameter is enforced.
        """
        Branch.objects.bulk_create((
            Branch(name='Branch 1', status=BranchStatusChoices.MERGED),
            Branch(name='Branch 2', status=BranchStatusChoices.READY),
        ))

        # Second active branch should be permitted (merged branches don't count)
        branch = Branch(name='Branch 3')
        branch.full_clean()
        branch.save()

        # Attempting to create a third active branch should fail
        branch = Branch(name='Branch 4')
        with self.assertRaises(ValidationError):
            branch.full_clean()

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'max_branches': 2,
        }
    })
    def test_max_branches(self):
        """
        Verify that the max_branches config parameter is enforced.
        """
        Branch.objects.bulk_create((
            Branch(name='Branch 1', status=BranchStatusChoices.ARCHIVED),
            Branch(name='Branch 2', status=BranchStatusChoices.READY),
        ))

        # Creating a second non-archived Branch should succeed
        branch = Branch(name='Branch 3')
        branch.full_clean()
        branch.save(provision=False)

        # Creating a third non-archived Branch should fail
        branch = Branch(name='Branch 4')
        with self.assertRaises(ValidationError):
            branch.full_clean()

    @override_settings(CUSTOM_VALIDATORS={
        'netbox_branching.branch': [
            CustomValidator({'name': {'min_length': 5}}),
        ],
    })
    def test_custom_validators_invoked(self):
        """
        Verify that CUSTOM_VALIDATORS configured against the Branch model are
        invoked during full_clean(). Regression test for issue #530.
        """
        # A name that violates the configured validator should fail
        branch = Branch(name='x')
        with self.assertRaises(ValidationError):
            branch.full_clean()

        # A name that satisfies the validator should pass
        branch = Branch(name='valid-name')
        branch.full_clean()

    @override_settings(CHANGELOG_RETENTION=10)
    def test_is_stale(self):
        branch = Branch(name='Branch 1')
        branch.save(provision=False)

        # Set creation time to 9 days in the past
        branch.last_sync = timezone.now() - timedelta(days=9)
        branch.save(update_merge_sync_fields=True)
        self.assertFalse(branch.is_stale)

        # Set creation time to 11 days in the past
        branch.last_sync = timezone.now() - timedelta(days=11)
        branch.save(update_merge_sync_fields=True)
        self.assertTrue(branch.is_stale)

    @override_settings(CHANGELOG_RETENTION=10)
    def test_stale_warning(self):
        branch = Branch(name='Branch 1')
        branch.save(provision=False)

        # Not yet in warning window (2 days ago, 8 days remaining > 7-day default threshold)
        branch.last_sync = timezone.now() - timedelta(days=2)
        branch.save(update_merge_sync_fields=True)
        self.assertIsNone(branch.stale_warning)

        # Within warning window (4 days ago, 6 days remaining <= 7-day default threshold)
        branch.last_sync = timezone.now() - timedelta(days=4)
        branch.save(update_merge_sync_fields=True)
        self.assertEqual(branch.stale_warning, 6)

        # Already stale (11 days ago) — warning should not show
        branch.last_sync = timezone.now() - timedelta(days=11)
        branch.save(update_merge_sync_fields=True)
        self.assertIsNone(branch.stale_warning)

    @override_settings(CHANGELOG_RETENTION=7)
    def test_stale_warning_threshold_equals_retention(self):
        """When stale_warning_threshold equals CHANGELOG_RETENTION, warning shows for all non-stale branches."""
        branch = Branch(name='Branch 1')
        branch.save(provision=False)

        branch.last_sync = timezone.now() - timedelta(days=6)
        branch.save(update_merge_sync_fields=True)
        self.assertEqual(branch.stale_warning, 1)

    def test_edit_form_preserves_lifecycle_fields(self):
        """
        Regression test for issue #445: editing a Branch through the form must not
        overwrite lifecycle fields (status, last_sync, etc.) that may have been
        updated by a background job after the form's instance was loaded.
        """
        branch = Branch(name='Branch 1')
        branch.save(provision=False)

        # Simulate a form instance loaded while status was still NEW
        stale_instance = Branch.objects.get(pk=branch.pk)
        self.assertEqual(stale_instance.status, BranchStatusChoices.NEW)

        # Concurrently, a background job updates lifecycle state
        sync_time = timezone.now() - timedelta(minutes=1)
        Branch.objects.filter(pk=branch.pk).update(
            status=BranchStatusChoices.READY,
            last_sync=sync_time,
        )

        # The user submits the edit form against the stale instance
        form = BranchForm(
            data={'name': 'Renamed Branch', 'description': '', 'comments': ''},
            instance=stale_instance,
        )
        self.assertTrue(form.is_valid(), msg=form.errors)
        form.save()

        # The name change persisted, lifecycle fields were not clobbered
        updated = Branch.objects.get(pk=branch.pk)
        self.assertEqual(updated.name, 'Renamed Branch')
        self.assertEqual(updated.status, BranchStatusChoices.READY)
        self.assertEqual(updated.last_sync, sync_time)

    def test_delete_transitional_branch_preserves_schema(self):
        """
        Regression test for issue #445: a delete attempt against a Branch in a
        transitional state must be blocked AND must not deprovision the schema.
        """
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)

        # Move the branch into a transitional state, as a background job would
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.PROVISIONING)
        branch.refresh_from_db()

        with self.assertRaises(AbortRequest):
            branch.delete()

        # The branch row must still exist
        self.assertTrue(Branch.objects.filter(pk=branch.pk).exists())

        # The schema must still exist
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [branch.schema_name]
            )
            self.assertIsNotNone(cursor.fetchone(), msg="Schema was unexpectedly dropped on blocked delete")

    def _assert_branch_and_schema_intact(self, branch_pk, schema_name):
        self.assertTrue(Branch.objects.filter(pk=branch_pk).exists())
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [schema_name]
            )
            self.assertIsNotNone(cursor.fetchone(), msg="Schema unexpectedly missing")

    def test_delete_rolls_back_row_when_deprovision_raises(self):
        """
        A failure raised inside deprovision() (before DROP SCHEMA runs) must roll back
        the row delete that super().delete() already performed, leaving both intact.
        """
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)

        # Capture identifiers up front: Django's Collector sets instance.pk to None
        # after running the DELETE, and that mutation is not undone by rollback.
        branch_pk = branch.pk
        schema_name = branch.schema_name

        def boom(sender, **kwargs):
            raise RuntimeError("simulated deprovision failure")

        pre_deprovision.connect(boom, sender=Branch, weak=False)
        try:
            with self.assertRaises(RuntimeError):
                branch.delete()
        finally:
            pre_deprovision.disconnect(boom, sender=Branch)

        self._assert_branch_and_schema_intact(branch_pk, schema_name)

    def test_delete_rolls_back_schema_drop_when_failure_follows(self):
        """
        If a failure is raised after DROP SCHEMA has already executed, the atomic
        block must roll back the DDL too — verifying the PostgreSQL DDL-is-transactional
        property the fix relies on.
        """
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)

        branch_pk = branch.pk
        schema_name = branch.schema_name

        def boom(sender, **kwargs):
            raise RuntimeError("simulated post-drop failure")

        post_deprovision.connect(boom, sender=Branch, weak=False)
        try:
            with self.assertRaises(RuntimeError):
                branch.delete()
        finally:
            post_deprovision.disconnect(boom, sender=Branch)

        self._assert_branch_and_schema_intact(branch_pk, schema_name)

    # -------------------------------------------------------------------------
    # Lifecycle action guards
    #
    # sync/merge/revert all check the branch's status and (for sync) staleness
    # before doing any work. The checks fire before pre_X signals, so a wrong
    # status raises an exception immediately. These tests pin down the guards
    # so a refactor of the lifecycle methods cannot silently relax them.
    # -------------------------------------------------------------------------

    def test_sync_raises_when_branch_not_ready(self):
        branch = Branch(name='Branch 1', status=BranchStatusChoices.FAILED)
        branch.save(provision=False)
        with self.assertRaisesRegex(Exception, 'not ready to sync'):
            branch.sync(user=None)

    def test_merge_raises_when_branch_not_ready(self):
        branch = Branch(name='Branch 1', status=BranchStatusChoices.SYNCING)
        branch.save(provision=False)
        with self.assertRaisesRegex(Exception, 'not ready to merge'):
            branch.merge(user=None)

    def test_revert_raises_when_branch_not_merged(self):
        branch = Branch(name='Branch 1', status=BranchStatusChoices.READY)
        branch.save(provision=False)
        with self.assertRaisesRegex(Exception, 'Only merged branches can be reverted'):
            branch.revert(user=None)

    @override_settings(CHANGELOG_RETENTION=10)
    def test_sync_raises_when_branch_is_stale(self):
        """
        is_stale is tested as a property elsewhere; this test confirms sync()
        enforces it as a precondition. Without this test, a refactor could
        accidentally drop the guard and let a stale branch attempt to apply
        changes whose ObjectChange rows have already been garbage-collected
        out of the main schema.
        """
        branch = Branch(name='Branch 1', status=BranchStatusChoices.READY)
        branch.save(provision=False)
        # Push last_sync past the CHANGELOG_RETENTION window
        Branch.objects.filter(pk=branch.pk).update(
            last_sync=timezone.now() - timedelta(days=11),
        )
        branch.refresh_from_db()
        self.assertTrue(branch.is_stale)
        with self.assertRaisesRegex(Exception, 'stale and can no longer be synced'):
            branch.sync(user=None)

    # -------------------------------------------------------------------------
    # Pre-action validators
    #
    # PLUGINS_CONFIG can register validators (e.g. sync_validators) that gate
    # the corresponding lifecycle action. The mechanism is loaded once in
    # AppConfig.ready(), but the underlying registration API is exposed for
    # direct use too. These tests cover the registration API; the AppConfig
    # path is exercised in production whenever the plugin loads.
    # -------------------------------------------------------------------------

    def test_preaction_validator_returning_false_blocks_action(self):
        def blocker(branch):
            return BranchActionIndicator(False, 'blocked by test')

        Branch.register_preaction_check(blocker, 'sync')
        try:
            branch = Branch(name='Branch 1', status=BranchStatusChoices.READY)
            branch.save(provision=False)
            indicator = branch.can_sync
            self.assertFalse(indicator)
            self.assertEqual(indicator.message, 'blocked by test')
        finally:
            Branch._preaction_validators['sync'].discard(blocker)

    def test_preaction_validator_blocks_sync_call_with_message(self):
        """can_sync gates sync(); a blocking validator must surface there too."""
        def blocker(branch):
            return BranchActionIndicator(False, 'blocked by test')

        Branch.register_preaction_check(blocker, 'sync')
        try:
            branch = Branch(name='Branch 1', status=BranchStatusChoices.READY)
            branch.save(provision=False)
            with self.assertRaisesRegex(Exception, 'not permitted'):
                branch.sync(user=None)
        finally:
            Branch._preaction_validators['sync'].discard(blocker)

    def test_preaction_validator_returning_falsy_non_indicator_is_wrapped(self):
        """
        Backwards compatibility: pre-v0.6.0 validators returned plain bools.
        _can_do_action wraps a falsy non-indicator return as
        BranchActionIndicator(False, ...). This protects integrations that
        still use the old contract.
        """
        def legacy_blocker(branch):
            return False

        Branch.register_preaction_check(legacy_blocker, 'merge')
        try:
            branch = Branch(name='Branch 1', status=BranchStatusChoices.READY)
            branch.save(provision=False)
            indicator = branch.can_merge
            self.assertFalse(indicator)
            self.assertIsInstance(indicator, BranchActionIndicator)
        finally:
            Branch._preaction_validators['merge'].discard(legacy_blocker)

    def test_register_preaction_check_rejects_unknown_action(self):
        def noop(branch):
            return BranchActionIndicator(True)

        with self.assertRaisesRegex(ValueError, 'Invalid branch action'):
            Branch.register_preaction_check(noop, 'not_a_real_action')


class BranchStatusDescriptionTestCase(SimpleTestCase):

    def test_descriptions_cover_all_statuses(self):
        # Every status choice must have a description, and vice versa.
        choice_values = {value for value, _ in BranchStatusChoices()}
        description_keys = set(BranchStatusChoices.DESCRIPTIONS)
        self.assertEqual(choice_values, description_keys)

    def test_get_status_description(self):
        branch = Branch(name='Branch 1', status=BranchStatusChoices.READY)
        self.assertEqual(
            branch.get_status_description(),
            BranchStatusChoices.DESCRIPTIONS[BranchStatusChoices.READY]
        )

    def test_get_status_description_unknown_status(self):
        branch = Branch(name='Branch 1', status='not-a-real-status')
        self.assertEqual(branch.get_status_description(), '')


class BranchProvisionPipelineTestCase(TransactionTestCase):
    """
    Targeted coverage of the parallel provisioning pipeline. The end-to-end
    happy path is exercised by BranchTestCase.test_create_branch; these tests
    pin down the specific guarantees Phase 1+2 changes make:
      * indexes on every replicated table are reproduced under their main-schema names
      * snapshot import is actually performed (not silently skipped)
      * worker failures cause schema cleanup and a FAILED branch
    """
    serialized_rollback = True

    def setUp(self):
        # Phase 1 of provision() commits the CREATE SCHEMA outside the test's
        # transaction, so TransactionTestCase rollback can't undo it. Track
        # any schemas these tests create and drop them in tearDown so --keepdb
        # runs don't accumulate orphans.
        super().setUp()
        self._provisioned_schemas = []

    def tearDown(self):
        for schema_name in self._provisioned_schemas:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS {quote_ident(schema_name)} CASCADE')
        super().tearDown()

    def _track(self, branch):
        self._provisioned_schemas.append(branch.schema_name)
        return branch

    def test_provision_preserves_every_main_schema_index(self):
        branch = self._track(Branch(name='IndexParity'))
        branch.save(provision=False)
        branch.provision(user=None)

        main_schema = get_plugin_config('netbox_branching', 'main_schema')
        relevant_tables = {*get_tables_to_replicate(), 'core_objectchange', 'django_migrations'}

        with connection.cursor() as cursor:
            # Pull all main indexes for the relevant tables, modulo SKIP_INDEXES.
            cursor.execute(
                "SELECT tablename, indexname FROM pg_indexes WHERE schemaname=%s",
                [main_schema],
            )
            expected = {
                (tbl, idx) for tbl, idx in cursor.fetchall()
                if tbl in relevant_tables and idx not in SKIP_INDEXES
            }

            cursor.execute(
                "SELECT tablename, indexname FROM pg_indexes WHERE schemaname=%s",
                [branch.schema_name],
            )
            found = set(cursor.fetchall())

        missing = expected - found
        self.assertFalse(
            missing,
            msg=f"Branch schema is missing {len(missing)} indexes that exist on main: {sorted(missing)[:5]}",
        )

    def test_provision_imports_exported_snapshot(self):
        """
        Phase 2 must call parallel_copy_tables with a non-empty snapshot token
        of the format pg_export_snapshot() returns.
        """
        from netbox_branching.models import branches as branches_module

        captured = {}
        original = branches_module.parallel_copy_tables

        def spy(*, tables, snapshot_token, schema, main_schema, workers):
            captured['token'] = snapshot_token
            captured['tables'] = list(tables)
            captured['workers'] = workers
            return original(
                tables=tables,
                snapshot_token=snapshot_token,
                schema=schema,
                main_schema=main_schema,
                workers=workers,
            )

        branches_module.parallel_copy_tables = spy
        try:
            branch = self._track(Branch(name='SnapshotImport'))
            branch.save(provision=False)
            branch.provision(user=None)
        finally:
            branches_module.parallel_copy_tables = original

        self.assertIn('token', captured, msg="parallel_copy_tables was never invoked")
        # pg_export_snapshot() returns digits and dashes (occasionally hex).
        self.assertRegex(captured['token'], r'\A[A-Fa-f0-9\-]+\Z')
        self.assertGreater(len(captured['tables']), 0)

    def test_provision_failure_drops_schema_and_marks_branch_failed(self):
        """
        Any exception out of the parallel pipeline must trigger DROP SCHEMA
        CASCADE and a FAILED branch status — matching the rollback semantics
        of the previous single-transaction implementation.
        """
        from netbox_branching.models import branches as branches_module

        original = branches_module.parallel_copy_tables

        def boom(*, tables, snapshot_token, schema, main_schema, workers):
            raise RuntimeError("simulated worker failure")

        branches_module.parallel_copy_tables = boom
        try:
            branch = self._track(Branch(name='FailureCleanup'))
            branch.save(provision=False)
            with self.assertRaisesRegex(RuntimeError, 'simulated worker failure'):
                branch.provision(user=None)
        finally:
            branches_module.parallel_copy_tables = original

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.FAILED)

        # The (partial) schema must have been dropped.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [branch.schema_name],
            )
            self.assertIsNone(cursor.fetchone(), msg="Partial schema was not cleaned up")

    def test_provision_phase3_failure_drops_schema_and_marks_branch_failed(self):
        """
        A failure in Phase 3 (constraint/index build) must hit the same cleanup
        path as a Phase 2 failure: DROP SCHEMA CASCADE + FAILED status. Phase 3
        runs after the schema and table data have already been committed, so this
        pins down that the outer except block cleans up a fully-populated schema —
        not just the empty-schema state a Phase 2 failure leaves behind.
        """
        from netbox_branching.models import branches as branches_module

        original = branches_module.parallel_build_indexes

        def boom(**kwargs):
            raise RuntimeError("simulated index build failure")

        branches_module.parallel_build_indexes = boom
        try:
            branch = self._track(Branch(name='Phase3FailureCleanup'))
            branch.save(provision=False)
            with self.assertRaisesRegex(RuntimeError, 'simulated index build failure'):
                branch.provision(user=None)
        finally:
            branches_module.parallel_build_indexes = original

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.FAILED)

        # The committed-then-populated schema must have been dropped.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [branch.schema_name],
            )
            self.assertIsNone(cursor.fetchone(), msg="Populated schema was not cleaned up")

    def test_provision_preserves_pk_and_unique_constraints(self):
        """
        Branch tables must end up with real PRIMARY KEY / UNIQUE / EXCLUDE
        constraints in pg_constraint, not just the underlying unique indexes.
        Future migrations that DROP CONSTRAINT by name depend on this.
        """
        branch = self._track(Branch(name='ConstraintParity'))
        branch.save(provision=False)
        branch.provision(user=None)

        main_schema = get_plugin_config('netbox_branching', 'main_schema')
        relevant_tables = {*get_tables_to_replicate(), 'core_objectchange', 'django_migrations'}

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cls.relname, con.conname, con.contype
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                WHERE ns.nspname = %s AND con.contype IN ('p', 'u', 'x')
                """,
                [main_schema],
            )
            expected = {
                (tbl, name, ctype) for tbl, name, ctype in cursor.fetchall()
                if tbl in relevant_tables
            }

            cursor.execute(
                """
                SELECT cls.relname, con.conname, con.contype
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                WHERE ns.nspname = %s AND con.contype IN ('p', 'u', 'x')
                """,
                [branch.schema_name],
            )
            found = set(cursor.fetchall())

        missing = expected - found
        self.assertFalse(
            missing,
            msg=f"Branch schema is missing {len(missing)} PK/UNIQUE/EXCLUDE constraints: {sorted(missing)[:5]}",
        )

    def test_cancel_backends_does_not_disturb_caller_connection(self):
        """
        _cancel_backends must operate on its own connection. The Phase 2
        coordinator's snapshot-exporting transaction lives on the main
        thread's connection while parallel_copy_tables runs; if cancellation
        closed that connection it would invalidate the snapshot before in-
        flight workers had a chance to observe their own failures, masking
        the original error.
        """
        from netbox_branching.provisioning import _cancel_backends

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            before_pid = cursor.fetchone()[0]

        # PID 0 is never a real backend; pg_cancel_backend returns false
        # without raising, exercising the cancellation path end-to-end.
        _cancel_backends([0])

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            after_pid = cursor.fetchone()[0]

        self.assertEqual(
            before_pid, after_pid,
            msg="_cancel_backends closed the caller's connection (PID changed)",
        )


class SnapshotTokenValidationTestCase(SimpleTestCase):
    """
    The Phase 2 snapshot token is interpolated directly into SET TRANSACTION
    SNAPSHOT because that utility statement does not accept bind parameters.
    The regex gate in parallel_copy_tables() is what keeps that interpolation
    safe; these tests pin the accepted character set so a future tweak can't
    accidentally widen it without someone noticing.
    """

    def test_regex_accepts_real_snapshot_tokens(self):
        from netbox_branching.provisioning import _SNAPSHOT_TOKEN_RE

        # Tokens of the shape pg_export_snapshot() actually returns.
        self.assertIsNotNone(_SNAPSHOT_TOKEN_RE.match('00000003-000000DC-1'))
        self.assertIsNotNone(_SNAPSHOT_TOKEN_RE.match('abc-def-123'))
        self.assertIsNotNone(_SNAPSHOT_TOKEN_RE.match('0'))

    def test_regex_rejects_injection_attempts(self):
        from netbox_branching.provisioning import _SNAPSHOT_TOKEN_RE

        for hostile in (
            "",
            "abc def",                    # whitespace
            "abc'; DROP TABLE foo; --",   # SQL injection
            "' OR 1=1 --",
            "abc\ndef",                   # newline
            "abç-def",                    # non-ASCII
            "abc/def",                    # path separator
        ):
            self.assertIsNone(
                _SNAPSHOT_TOKEN_RE.match(hostile),
                msg=f"Regex unexpectedly accepted {hostile!r}",
            )

    def test_parallel_copy_tables_refuses_bad_token(self):
        from netbox_branching.provisioning import parallel_copy_tables

        with self.assertRaisesRegex(ValueError, 'unexpected snapshot token format'):
            parallel_copy_tables(
                tables=['some_table'],
                snapshot_token="'; DROP TABLE foo; --",
                schema='branch_xxx',
                main_schema='public',
                workers=1,
            )
