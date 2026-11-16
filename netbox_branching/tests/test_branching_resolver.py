"""
Tests for the ``register_branching_resolver`` plugin extension point.

The resolver list lives at module level on ``netbox_branching.utilities``;
each test snapshots and restores it so registrations don't leak between
tests.
"""

import importlib
from contextlib import contextmanager
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth.models import Group, Permission
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connection, migrations, models, transaction
from django.test import TestCase, override_settings

from ipam.models import Prefix
from netbox_branching import utilities
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models.branches import _alters_only_unreplicated_models, _skip_unreplicated_operations
from netbox_branching.signal_receivers import check_pending_migrations
from netbox_branching.tests.utils import FastTeardownTransactionTestCase, provision_branch
from netbox_branching.utilities import (
    activate_branch,
    get_tables_to_replicate,
    register_branching_resolver,
    supports_branching,
)


@contextmanager
def _isolated_resolvers():
    """Snapshot ``_branching_resolvers`` and restore it on exit."""
    saved = list(utilities._branching_resolvers)
    utilities._branching_resolvers.clear()
    try:
        yield
    finally:
        utilities._branching_resolvers.clear()
        utilities._branching_resolvers.extend(saved)


class RegisterBranchingResolverTestCase(TestCase):
    """``register_branching_resolver`` input validation."""

    def test_register_callable(self):
        with _isolated_resolvers():

            def resolver(model):
                return None

            register_branching_resolver(resolver)
            self.assertIn(resolver, utilities._branching_resolvers)

    def test_register_non_callable_raises(self):
        with _isolated_resolvers():
            with self.assertRaises(TypeError):
                register_branching_resolver('not callable')
            with self.assertRaises(TypeError):
                register_branching_resolver(42)
            with self.assertRaises(TypeError):
                register_branching_resolver(None)
            self.assertEqual(utilities._branching_resolvers, [])

    def test_register_lambda(self):
        with _isolated_resolvers():
            register_branching_resolver(lambda model: None)
            self.assertEqual(len(utilities._branching_resolvers), 1)

    def test_multiple_registrations_preserve_order(self):
        with _isolated_resolvers():

            def r1(model):
                return None

            def r2(model):
                return None

            def r3(model):
                return None

            register_branching_resolver(r1)
            register_branching_resolver(r2)
            register_branching_resolver(r3)
            self.assertEqual(utilities._branching_resolvers, [r1, r2, r3])


class ResolverDispatchTestCase(TestCase):
    """``supports_branching`` integration with registered resolvers."""

    def test_resolver_returning_true_opts_model_in(self):
        """A resolver returning True branches a model that wouldn't be by default.

        ``auth.Group`` is not a ChangeLoggingMixin subclass and not in
        INCLUDE_MODELS, so supports_branching returns False by default.  A
        resolver returning True should flip that result.
        """
        # Baseline: not branchable without resolver
        self.assertFalse(supports_branching(Group))

        with _isolated_resolvers():

            def resolver(model):
                if model is Group:
                    return True
                return None

            register_branching_resolver(resolver)
            self.assertTrue(supports_branching(Group))

    def test_resolver_returning_false_opts_model_out(self):
        """A resolver returning False excludes a model that's branchable by default.

        ``ipam.Prefix`` is a ChangeLoggingMixin subclass so supports_branching
        returns True by default.  A resolver returning False should override
        that decision.
        """
        # Baseline: branchable without resolver
        self.assertTrue(supports_branching(Prefix))

        with _isolated_resolvers():

            def resolver(model):
                if model is Prefix:
                    return False
                return None

            register_branching_resolver(resolver)
            self.assertFalse(supports_branching(Prefix))

    def test_resolver_returning_none_falls_through(self):
        """A resolver returning None should not affect the default decision."""
        with _isolated_resolvers():

            def resolver(model):
                return None

            register_branching_resolver(resolver)
            # ChangeLoggingMixin-based model still branchable
            self.assertTrue(supports_branching(Prefix))
            # Non-ChangeLoggingMixin model still excluded
            self.assertFalse(supports_branching(Group))

    def test_first_non_none_wins(self):
        """Resolvers run in registration order; first non-None decides."""
        with _isolated_resolvers():
            register_branching_resolver(lambda m: None)  # defers
            register_branching_resolver(lambda m: True)  # decides
            register_branching_resolver(lambda m: False)  # never reached
            self.assertTrue(supports_branching(Group))

    def test_raising_resolver_is_swallowed(self):
        """A resolver that raises is logged and treated as None."""

        def bad_resolver(model):
            raise RuntimeError('boom')

        with _isolated_resolvers():
            register_branching_resolver(bad_resolver)
            with self.assertLogs('netbox_branching.utilities', level='ERROR') as cm:
                # Falls through to default heuristic.
                self.assertTrue(supports_branching(Prefix))
                self.assertFalse(supports_branching(Group))
            joined = '\n'.join(cm.output)
            self.assertIn('boom', joined)
            self.assertIn('treating as None', joined)

    def test_raising_resolver_does_not_block_subsequent_resolvers(self):
        """A raising resolver should not prevent the next resolver from running."""

        def bad_resolver(model):
            raise RuntimeError('boom')

        with _isolated_resolvers():
            register_branching_resolver(bad_resolver)
            register_branching_resolver(lambda m: True if m is Group else None)
            with self.assertLogs('netbox_branching.utilities', level='ERROR'):
                self.assertTrue(supports_branching(Group))

    def test_include_models_takes_precedence_over_resolver(self):
        """``INCLUDE_MODELS`` runs before resolvers; a False resolver can't override it.

        ``extras.taggeditem`` is in INCLUDE_MODELS, so even a resolver returning
        False should not exclude it.
        """
        from extras.models import TaggedItem

        with _isolated_resolvers():
            register_branching_resolver(lambda m: False)
            self.assertTrue(supports_branching(TaggedItem))

    def test_exempt_models_filter_still_runs_after_resolver(self):
        """A resolver returning True is still subject to ``exempt_models``."""
        from django.test import override_settings

        with _isolated_resolvers():
            register_branching_resolver(lambda m: True if m is Group else None)
            # Without exempt_models: resolver opts Group in.
            self.assertTrue(supports_branching(Group))
            # With exempt_models matching Group: exempt wins.
            with override_settings(
                PLUGINS_CONFIG={
                    'netbox_branching': {'exempt_models': ['auth.group']},
                }
            ):
                self.assertFalse(supports_branching(Group))


def _group_resolver(model):
    return True if model is Group else None


GroupPermissions = Group.permissions.through
GROUP_PERMISSIONS_TABLE = GroupPermissions._meta.db_table


class ResolverTableReplicationTestCase(TestCase):
    """``get_tables_to_replicate`` includes the tables of models a resolver makes branchable."""

    def test_resolver_model_table_is_replicated(self):
        self.assertNotIn(Group._meta.db_table, get_tables_to_replicate())
        with _isolated_resolvers():
            register_branching_resolver(_group_resolver)
            tables = get_tables_to_replicate()
            self.assertIn(Group._meta.db_table, tables)
            self.assertIn(GROUP_PERMISSIONS_TABLE, tables)

    def test_exempt_resolver_model_table_is_not_replicated(self):
        with _isolated_resolvers():
            register_branching_resolver(_group_resolver)
            with override_settings(PLUGINS_CONFIG={'netbox_branching': {'exempt_models': ['auth.group']}}):
                tables = get_tables_to_replicate()
                self.assertNotIn(Group._meta.db_table, tables)
                self.assertNotIn(GROUP_PERMISSIONS_TABLE, tables)


class ResolverModelBranchTestCase(FastTeardownTransactionTestCase):
    """A branch provisioned with a resolver registered gets its own copy of the model's table."""

    serialized_rollback = True

    def test_changes_in_branch_stay_in_branch(self):
        Group.objects.create(name='Group A')
        permission = Permission.objects.first()
        with _isolated_resolvers():
            register_branching_resolver(_group_resolver)
            branch = provision_branch(user=None, name='Resolver Branch')

            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s',
                    [branch.schema_name, Group._meta.db_table],
                )
                self.assertIsNotNone(cursor.fetchone())

            with activate_branch(branch):
                group = Group.objects.get(name='Group A')
                group.name = 'Group B'
                group.save()
                group.permissions.add(permission)
                self.assertTrue(Group.objects.filter(name='Group B').exists())
                # Query the through table directly: NetBox's users.Group also has a permissions relation,
                # and group.permissions.filter() joins users_group_permissions instead.
                self.assertTrue(
                    GroupPermissions.objects.using(group._state.db).filter(group=group, permission=permission).exists()
                )

        self.assertTrue(Group.objects.filter(name='Group A').exists())
        self.assertFalse(Group.objects.filter(name='Group B').exists())
        self.assertFalse(GroupPermissions.objects.filter(group__name='Group A').exists())


def _group_label_resolver(model):
    # Match by label so that the historical models of data migrations resolve too
    return True if model._meta.label_lower == 'auth.group' else None


AUTH_0010 = importlib.import_module('django.contrib.auth.migrations.0010_alter_group_name_max_length').Migration
AUTH_0011 = importlib.import_module('django.contrib.auth.migrations.0011_update_proxy_permissions').Migration


def _table_exists(schema, table):
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s',
            [schema, table],
        )
        return cursor.fetchone() is not None


class NewlyBranchableModelMigrateTestCase(FastTeardownTransactionTestCase):
    """Migrating a branch provisioned before a model became branchable gives it its own copy of the table. (#683)"""

    serialized_rollback = True

    def _leave_auth_migrations_pending(self, branch):
        # Leave auth.0010 (AlterField on Group.name) pending in the branch. Django plans nothing for an app whose
        # leaf migration is applied, so the later ones must be pending too.
        with connection.cursor() as cursor:
            cursor.execute(
                f'DELETE FROM "{branch.schema_name}".django_migrations WHERE app = %s AND name >= %s',
                ['auth', '0010'],
            )

    def test_migrate_replicates_table(self):
        Group.objects.create(name='Group A')
        branch = provision_branch(user=None, name='Pre-resolver Branch')
        self.assertFalse(_table_exists(branch.schema_name, Group._meta.db_table))
        self._leave_auth_migrations_pending(branch)

        with _isolated_resolvers():
            register_branching_resolver(_group_resolver)
            branch.migrate(user=None)

            branch.refresh_from_db()
            self.assertEqual(branch.status, BranchStatusChoices.READY)
            self.assertIn('auth.0010_alter_group_name_max_length', branch.applied_migrations)
            self.assertTrue(_table_exists(branch.schema_name, Group._meta.db_table))
            self.assertTrue(_table_exists(branch.schema_name, GROUP_PERMISSIONS_TABLE))

            with activate_branch(branch):
                self.assertTrue(Group.objects.filter(name='Group A').exists())
                Group.objects.create(name='Group B')
                # Main's constraints were copied
                with self.assertRaises(IntegrityError), transaction.atomic(using=branch.connection_name):
                    Group.objects.create(name='Group A')

        self.assertFalse(Group.objects.filter(name='Group B').exists())

    def test_migrate_without_pending_migrations(self):
        branch = provision_branch(user=None, name='Pre-resolver Branch')

        with _isolated_resolvers():
            register_branching_resolver(_group_resolver)
            self.assertEqual(branch.pending_migrations, [])
            check_pending_migrations(sender=django_apps.get_app_config('netbox_branching'), using=DEFAULT_DB_ALIAS)
            branch.refresh_from_db()
            self.assertEqual(branch.status, BranchStatusChoices.PENDING_MIGRATIONS)

            branch.migrate(user=None)
            branch.refresh_from_db()
            self.assertEqual(branch.status, BranchStatusChoices.READY)
            self.assertTrue(_table_exists(branch.schema_name, Group._meta.db_table))

    def test_failed_copy_can_be_retried(self):
        branch = provision_branch(user=None, name='Pre-resolver Branch')
        self._leave_auth_migrations_pending(branch)

        with _isolated_resolvers():
            register_branching_resolver(_group_resolver)
            with (
                patch('netbox_branching.models.branches.parallel_copy_tables', side_effect=RuntimeError('boom')),
                self.assertRaises(RuntimeError),
            ):
                branch.migrate(user=None)

            branch.refresh_from_db()
            self.assertEqual(branch.status, BranchStatusChoices.PENDING_MIGRATIONS)
            self.assertNotIn('auth.0010_alter_group_name_max_length', branch.applied_migrations)
            self.assertFalse(_table_exists(branch.schema_name, Group._meta.db_table))

            branch.migrate(user=None)
            branch.refresh_from_db()
            self.assertEqual(branch.status, BranchStatusChoices.READY)
            self.assertTrue(_table_exists(branch.schema_name, Group._meta.db_table))

    def _migrate_with_operations(self, branch, operations, later_operations=()):
        # Append operations to auth.0010 (and auth.0011), which main has already applied
        with (
            _isolated_resolvers(),
            patch.object(AUTH_0010, 'operations', [*AUTH_0010.operations, *operations]),
            patch.object(AUTH_0011, 'operations', [*AUTH_0011.operations, *later_operations]),
        ):
            register_branching_resolver(_group_label_resolver)
            branch.migrate(user=None)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)

    def test_data_migration_not_rerun_on_copy(self):
        Group.objects.create(name='Group A')
        branch = provision_branch(user=None, name='Pre-resolver Branch')
        self._leave_auth_migrations_pending(branch)

        def rename_groups(apps, schema_editor):
            for group in apps.get_model('auth', 'Group').objects.all():
                group.name += ' (migrated)'
                group.save()

        self._migrate_with_operations(branch, [migrations.RunPython(rename_groups)])
        # The copy already holds main's migrated rows
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT name FROM "{branch.schema_name}".auth_group')
            self.assertEqual(cursor.fetchall(), [('Group A',)])
        self.assertTrue(Group.objects.filter(name='Group A').exists())

    def test_data_migration_on_column_main_has_removed(self):
        Group.objects.create(name='Group A')
        branch = provision_branch(user=None, name='Pre-resolver Branch')
        self._leave_auth_migrations_pending(branch)

        def read_groups(apps, schema_editor):
            list(apps.get_model('auth', 'Group').objects.all())

        self._migrate_with_operations(
            branch,
            [migrations.AddField('group', 'tmp', models.IntegerField(null=True)), migrations.RunPython(read_groups)],
            later_operations=[migrations.RemoveField('group', 'tmp')],
        )

    def test_runsql_already_applied_on_main(self):
        branch = provision_branch(user=None, name='Pre-resolver Branch')
        self._leave_auth_migrations_pending(branch)

        self._migrate_with_operations(
            branch, [migrations.RunSQL('ALTER TABLE auth_group ADD CONSTRAINT auth_group_name_key UNIQUE (name)')]
        )


class SkipUnreplicatedOperationsTestCase(TestCase):
    """Operations on branchable models whose table the branch lacks are skipped. (#683)"""

    def _migration(self, app_label, operations):
        migration = migrations.Migration('9999_test', app_label)
        migration.operations = operations
        return migration

    def test_alters_only_unreplicated_models(self):
        add_field = migrations.AddField('site', 'foo', models.IntegerField(null=True))
        add_index = migrations.AddIndex('region', models.Index(fields=['name'], name='test_idx'))
        run_python = migrations.RunPython(migrations.RunPython.noop)
        separate = migrations.SeparateDatabaseAndState(database_operations=[add_field])

        def alters_only(app_label, operations):
            return _alters_only_unreplicated_models(self._migration(app_label, operations), {('dcim', 'site')})

        self.assertTrue(alters_only('dcim', [add_field, run_python]))
        self.assertTrue(alters_only('dcim', [separate]))
        self.assertFalse(alters_only('dcim', [add_field, add_index]))
        self.assertFalse(alters_only('dcim', [run_python]))
        self.assertFalse(alters_only('ipam', [add_field]))

    def test_unreplicated_operations_only_update_state(self):
        add_field = migrations.AddField('site', 'foo', models.IntegerField(null=True))
        add_index = migrations.AddIndex('region', models.Index(fields=['name'], name='test_idx'))
        migration = self._migration('dcim', [add_field, add_index])

        skipped = _skip_unreplicated_operations(migration, frozenset({('dcim', 'site')}))
        self.assertIsInstance(skipped.operations[0], migrations.SeparateDatabaseAndState)
        self.assertEqual(skipped.operations[0].state_operations, [add_field])
        self.assertEqual(skipped.operations[0].database_operations, [])
        self.assertIs(skipped.operations[1], add_index)
        # The loaded migration is left untouched
        self.assertEqual(migration.operations, [add_field, add_index])
        self.assertIs(_skip_unreplicated_operations(migration, frozenset()), migration)

    def test_nested_database_operations_skipped(self):
        add_field = migrations.AddField('site', 'foo', models.IntegerField(null=True))
        add_index = migrations.AddIndex('region', models.Index(fields=['name'], name='test_idx'))
        separate = migrations.SeparateDatabaseAndState(database_operations=[add_field, add_index])
        migration = self._migration('dcim', [separate])

        skipped = _skip_unreplicated_operations(migration, frozenset({('dcim', 'site')}))
        self.assertEqual(skipped.operations[0].database_operations, [add_index])
        self.assertEqual(skipped.operations[0].state_operations, [])
        self.assertEqual(separate.database_operations, [add_field, add_index])
