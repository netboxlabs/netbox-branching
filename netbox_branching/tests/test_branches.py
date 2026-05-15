import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from extras.validators import CustomValidator
from netbox.plugins import get_plugin_config
from utilities.exceptions import AbortRequest

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import SKIP_INDEXES
from netbox_branching.forms import BranchForm
from netbox_branching.models import Branch
from netbox_branching.signals import pre_deprovision
from netbox_branching.utilities import get_tables_to_replicate

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

    def test_delete_rolls_back_when_deprovision_fails(self):
        """
        If deprovision() raises after super().delete() has already removed the row,
        the atomic block must roll back the row delete and the schema drop together,
        leaving both the Branch row and its schema intact.
        """
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)

        # Capture identifiers up front — Django's Collector sets instance.pk to None
        # after running the DELETE, and that mutation is not undone by a rollback.
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

        # The branch row must still exist (atomic rolled back the super().delete())
        self.assertTrue(Branch.objects.filter(pk=branch_pk).exists())

        # The schema must still exist (DROP SCHEMA never executed)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [schema_name]
            )
            self.assertIsNotNone(cursor.fetchone(), msg="Schema was unexpectedly dropped despite failed deprovision")
