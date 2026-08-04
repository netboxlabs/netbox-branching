"""
Unit tests for the pluggable branching backend seam.

``BranchingBackend`` is the contract that isolates the *mechanism* of branch
isolation (create/destroy the dataset, name and configure its connection, apply
migrations to it) from everything built on top of it. The tests here cover:

  * ``get_branching_backend()`` resolution, caching and cache invalidation
  * ``SchemaBranchingBackend``'s connection addressing and configuration validation
    — the assertions previously made implicitly by app startup and by
    ``DynamicSchemaDictTestCase``, now pinned at the backend level
  * that ``Branch`` and ``BranchAwareRouter`` genuinely delegate through the
    backend rather than reaching for schema internals directly

Real schema DDL is exercised by ``test_branches.py``; nothing here provisions a
branch.
"""
from unittest import mock

from dcim.models import Site
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from netbox_branching import backends
from netbox_branching.backends import BranchingBackend, SchemaBranchingBackend, get_branching_backend
from netbox_branching.choices import BranchEventTypeChoices, BranchStatusChoices
from netbox_branching.contextvars import active_branch
from netbox_branching.database import BranchAwareRouter
from netbox_branching.models import Branch, BranchEvent
from netbox_branching.signals import (
    post_deprovision,
    post_migrate,
    post_provision,
    pre_deprovision,
    pre_migrate,
    pre_provision,
)
from netbox_branching.utilities import supports_branching

DUMMY_BACKEND = 'netbox_branching.tests.test_backends.DummyBranchingBackend'


class DummyBranchingBackend(BranchingBackend):
    """
    A backend that records the calls made to it instead of touching the database.
    Used to prove that Branch and BranchAwareRouter delegate rather than
    re-implement.

    Instances are created by ``get_branching_backend()``, which caches one per import
    path, so the call log is per-instance and is read back via
    ``get_branching_backend()`` rather than from a fixture.
    """
    connection_alias_prefix = 'dummy_'

    def __init__(self):
        self.calls = []
        # Branch statuses observed from inside provision(), so a test can assert on
        # the status transition Branch.provision() performs around the backend call.
        self.status_during_provision = None

    def provision(self, branch, user):
        self.calls.append(('provision', branch.pk, user))
        if not branch.backend_id:
            branch.set_backend_id(f'dummy-{branch.pk}')
        self.status_during_provision = Branch.objects.get(pk=branch.pk).status

    def deprovision(self, branch):
        self.calls.append(('deprovision', branch.pk))

    def get_connection_alias(self, branch):
        return f'{self.connection_alias_prefix}{branch.backend_id}'

    def get_connection_config(self, alias, default_config):
        if not self.owns_connection_alias(alias):
            return None
        return dict(default_config)

    def get_pending_migrations(self, branch):
        self.calls.append(('get_pending_migrations', branch.pk))
        return [('dcim', '9999_dummy')]

    def apply_migrations(self, branch, progress_callback=None):
        self.calls.append(('apply_migrations', branch.pk))
        if progress_callback:
            progress_callback('apply_start', migration='dcim.9999_dummy')
            progress_callback('apply_success', migration='dcim.9999_dummy')


class NotABackend:
    pass


class GetBranchingBackendTestCase(TestCase):

    def test_default_backend_is_the_schema_backend(self):
        self.assertIsInstance(get_branching_backend(), SchemaBranchingBackend)

    def test_instance_is_cached(self):
        self.assertIs(get_branching_backend(), get_branching_backend())

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend': DUMMY_BACKEND}})
    def test_configured_backend_is_honoured(self):
        self.assertIsInstance(get_branching_backend(), DummyBranchingBackend)

    def test_cache_is_invalidated_by_override_settings(self):
        """
        A backend may memoize configuration-derived state, so the cached instance
        must not survive a settings change. Without this, an override_settings()
        block would silently keep using an instance built from the old settings.
        """
        original = get_branching_backend()
        with override_settings(PLUGINS_CONFIG={'netbox_branching': {}}):
            self.assertIsNot(get_branching_backend(), original)

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend': 'nonexistent.module.Backend'}})
    def test_unimportable_backend_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            get_branching_backend()

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {'backend': 'netbox_branching.tests.test_backends.NotABackend'},
    })
    def test_non_backend_class_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            get_branching_backend()

    def test_not_required_matches_required_when_plugin_enabled(self):
        self.assertIs(get_branching_backend(required=False), get_branching_backend())

    @override_settings(PLUGINS_CONFIG={})
    def test_not_required_returns_none_when_plugin_disabled(self):
        """
        DATABASES and DATABASE_ROUTERS are host configuration and stay wired up when the
        plugin is dropped from PLUGINS, so the shims that read them must be able to ask
        for a backend and be told there isn't one — rather than get_plugin_config()'s
        "Plugin netbox_branching is not registered."
        """
        with self.assertRaises(ImproperlyConfigured):
            get_branching_backend()
        self.assertIsNone(get_branching_backend(required=False))

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend': 'nonexistent.module.Backend'}})
    def test_not_required_still_raises_on_a_bad_backend(self):
        """
        Only the plugin's absence is graceful. An enabled plugin naming an unimportable
        backend must fail loudly rather than degrade to no branching at all.
        """
        with self.assertRaises(ImproperlyConfigured):
            get_branching_backend(required=False)


class SchemaBackendIdentityTestCase(TestCase):
    """
    The schema backend owns the generation of a branch's identifier, because the
    identifier is what names the branch's schema. It is assigned during
    provisioning, not when the Branch is created.
    """

    def setUp(self):
        self.backend = SchemaBranchingBackend()

    def test_generated_id_is_eight_alphanumerics(self):
        self.assertRegex(self.backend.generate_branch_id(), r'^[a-z0-9]{8}$')

    def test_generated_ids_avoid_existing_branches(self):
        """
        Generation runs with database access, so it can rule out a collision rather
        than relying on the unique constraint to surface one.
        """
        taken = 'aaaaaaaa'
        Branch(name='Existing', backend_id=taken).save(provision=False)
        with mock.patch(
            'netbox_branching.backends.schema.random.choices',
            side_effect=[list(taken), list('bbbbbbbb')],
        ):
            self.assertEqual(self.backend.generate_branch_id(), 'bbbbbbbb')

    def test_generation_gives_up_rather_than_looping_forever(self):
        taken = 'aaaaaaaa'
        Branch(name='Existing', backend_id=taken).save(provision=False)
        with (
            mock.patch(
                'netbox_branching.backends.schema.random.choices', return_value=list(taken)
            ),
            self.assertRaisesRegex(RuntimeError, 'unique branch ID'),
        ):
            self.backend.generate_branch_id()

    def test_connection_alias_requires_a_provisioned_branch(self):
        """
        Without an identifier there is no schema name, and composing an alias anyway
        would give every unprovisioned branch the same bogus connection.
        """
        branch = Branch(name='Unprovisioned Branch')
        with self.assertRaises(ValueError):
            self.backend.get_connection_alias(branch)

    def test_deprovision_is_a_noop_without_an_identifier(self):
        """
        Branch.delete() reaches deprovision() even for a branch whose provisioning
        never got as far as assigning an identifier.
        """
        branch = Branch(name='Unprovisioned Branch')
        branch.save(provision=False)
        self.backend.deprovision(branch)  # must not raise

    def test_overlong_schema_name_is_rejected(self):
        """
        PostgreSQL truncates identifiers past 63 bytes, so two branches whose schema
        names differ only beyond that point would silently share one schema.
        """
        branch = Branch(name='Long ID Branch', backend_id='x' * 200)
        branch.save(provision=False)
        with self.assertRaisesRegex(ImproperlyConfigured, 'exceeds'):
            self.backend.provision(branch, user=None)


class SchemaBackendConnectionTestCase(TestCase):
    """
    Connection addressing must round-trip: the alias the backend hands the router
    is the same one ``DynamicSchemaDict`` will be asked to configure.
    """

    def setUp(self):
        self.backend = SchemaBranchingBackend()
        self.branch = Branch(name='Backend Test Branch', backend_id='bckndtst')

    def test_connection_alias_matches_branch_connection_name(self):
        alias = self.backend.get_connection_alias(self.branch)
        self.assertEqual(alias, f'schema_{self.branch.schema_name}')
        self.assertEqual(alias, self.branch.connection_name)

    def test_owns_connection_alias(self):
        self.assertTrue(self.backend.owns_connection_alias(self.branch.connection_name))
        self.assertFalse(self.backend.owns_connection_alias('default'))
        self.assertFalse(self.backend.owns_connection_alias(None))

    def test_connection_config_sets_search_path(self):
        default_config = {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'netbox',
            'OPTIONS': {'sslmode': 'require'},
        }
        config = self.backend.get_connection_config('schema_branch_abc123', default_config)
        self.assertEqual(config, {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'netbox',
            'OPTIONS': {
                'sslmode': 'require',
                'options': '-c search_path=branch_abc123,public',
            },
        })

    def test_connection_config_returns_none_for_non_branch_alias(self):
        """
        Returning None is what lets DynamicSchemaDict fall through to the real
        dict entry for 'default' (and raise KeyError for anything unknown).
        """
        self.assertIsNone(self.backend.get_connection_config('default', {}))
        # A bare prefix has no schema name, so it is not a usable branch alias.
        self.assertIsNone(self.backend.get_connection_config('schema_', {}))

    def test_routes_model_defaults_to_supports_branching(self):
        self.assertTrue(self.backend.routes_model(Site, self.branch))
        self.assertEqual(self.backend.routes_model(Site, self.branch), supports_branching(Site))
        # The plugin's own models are never branchable.
        self.assertFalse(self.backend.routes_model(Branch, self.branch))

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'exempt_models': ['dcim.site']}})
    def test_routes_model_respects_exempt_models(self):
        self.assertFalse(self.backend.routes_model(Site, self.branch))


class SchemaBackendValidationTestCase(TestCase):
    """
    ``validate_configuration()`` holds the checks that used to live inline in
    AppConfig.ready(). They were previously only asserted implicitly — a broken
    host configuration surfaced as a failure to start NetBox at all, which is not
    something a test can observe.
    """

    def setUp(self):
        self.backend = SchemaBranchingBackend()

    def test_valid_configuration_passes(self):
        self.backend.validate_configuration()  # must not raise

    def test_plain_databases_dict_raises(self):
        # Patched directly rather than via override_settings: overriding DATABASES
        # makes Django tear down and rebuild every connection, which would break the
        # transaction this test runs inside.
        with (
            mock.patch.object(settings, 'DATABASES', {'default': {}}),
            self.assertRaises(ImproperlyConfigured),
        ):
            self.backend.validate_configuration()

    @override_settings(DATABASE_ROUTERS=[])
    def test_missing_router_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            self.backend.validate_configuration()

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'provision_workers': 'four'}})
    def test_non_integer_provision_workers_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            self.backend.validate_configuration()

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'provision_workers': 0}})
    def test_zero_provision_workers_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            self.backend.validate_configuration()

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'provision_workers': None}})
    def test_none_provision_workers_is_permitted(self):
        self.backend.validate_configuration()  # must not raise


class ConnectionParamsRegistryTestCase(TestCase):
    """
    The registry exists so a backend needing per-branch endpoint details can stash
    them in get_connection_alias() (where a Branch row is in hand) and read them
    back in get_connection_config() (which must not query the database).
    """

    def test_register_and_retrieve(self):
        backend = SchemaBranchingBackend()
        backend.register_connection_params('schema_branch_reg', {'HOST': 'replica.example.com'})
        self.assertEqual(
            backend.get_registered_connection_params('schema_branch_reg'),
            {'HOST': 'replica.example.com'},
        )

    def test_unregistered_alias_returns_none(self):
        backend = SchemaBranchingBackend()
        self.assertIsNone(backend.get_registered_connection_params('schema_branch_never_seen'))


@override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend': DUMMY_BACKEND}})
class BackendDelegationTestCase(TestCase):
    """
    Every branch operation whose implementation moved into the backend must
    actually route through it, and Branch must retain the surrounding lifecycle
    behaviour (status transitions, signals, BranchEvents) regardless of backend.
    """

    def setUp(self):
        self.backend = get_branching_backend()
        self.assertIsInstance(self.backend, DummyBranchingBackend)
        self.branch = Branch(name='Delegation Branch', backend_id='delegatn')
        self.branch.save(provision=False)

    def _capture(self, signal):
        """Connect a receiver to ``signal`` and return the list it appends to."""
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        signal.connect(receiver, sender=Branch, weak=False)
        self.addCleanup(signal.disconnect, receiver, sender=Branch)
        return received

    def test_connection_name_comes_from_backend(self):
        self.assertEqual(self.branch.connection_name, f'dummy_{self.branch.backend_id}')

    def test_provision_delegates_and_preserves_lifecycle(self):
        pre = self._capture(pre_provision)
        post = self._capture(post_provision)

        self.branch.provision(user=None)

        self.assertIn(('provision', self.branch.pk, None), self.backend.calls)
        # Branch.provision() owns the status transitions around the backend call.
        self.assertEqual(self.backend.status_during_provision, BranchStatusChoices.PROVISIONING)
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatusChoices.READY)
        self.assertIsNotNone(self.branch.last_sync)

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)
        self.assertTrue(
            BranchEvent.objects.filter(
                branch=self.branch, type=BranchEventTypeChoices.PROVISIONED
            ).exists()
        )

    def test_provision_failure_marks_branch_failed(self):
        with (
            mock.patch.object(
                DummyBranchingBackend, 'provision', side_effect=RuntimeError('backend exploded')
            ),
            self.assertRaisesRegex(RuntimeError, 'backend exploded'),
        ):
            self.branch.provision(user=None)

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatusChoices.FAILED)

    def test_deprovision_delegates_and_emits_signals(self):
        pre = self._capture(pre_deprovision)
        post = self._capture(post_deprovision)

        self.branch.deprovision()

        self.assertIn(('deprovision', self.branch.pk), self.backend.calls)
        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)

    def test_pending_migrations_delegates(self):
        self.assertEqual(self.branch.pending_migrations, [('dcim', '9999_dummy')])
        self.assertIn(('get_pending_migrations', self.branch.pk), self.backend.calls)

    def test_migrate_delegates_and_preserves_lifecycle(self):
        pre = self._capture(pre_migrate)
        post = self._capture(post_migrate)

        self.branch.migrate(user=None)

        self.assertIn(('apply_migrations', self.branch.pk), self.backend.calls)
        # The progress callback stays in Branch.migrate(), so applied_migrations is
        # populated from whatever the backend reports.
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.applied_migrations, ['dcim.9999_dummy'])
        self.assertEqual(self.branch.status, BranchStatusChoices.READY)

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)
        self.assertTrue(
            BranchEvent.objects.filter(
                branch=self.branch, type=BranchEventTypeChoices.MIGRATED
            ).exists()
        )

    def test_migrate_failure_marks_branch_failed(self):
        with (
            mock.patch.object(
                DummyBranchingBackend, 'apply_migrations', side_effect=RuntimeError('migration exploded')
            ),
            self.assertRaisesRegex(RuntimeError, 'migration exploded'),
        ):
            self.branch.migrate(user=None)

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatusChoices.FAILED)

    def test_router_uses_backend_alias(self):
        router = BranchAwareRouter()
        token = active_branch.set(self.branch)
        self.addCleanup(active_branch.reset, token)

        expected = f'dummy_{self.branch.backend_id}'
        self.assertEqual(router.db_for_read(Site), expected)
        self.assertEqual(router.db_for_write(Site), expected)

    def test_router_respects_backend_routes_model(self):
        """
        routes_model() is the backend's say over which models reach the branch;
        the router must consult it rather than calling supports_branching() itself.
        """
        router = BranchAwareRouter()
        token = active_branch.set(self.branch)
        self.addCleanup(active_branch.reset, token)

        with mock.patch.object(DummyBranchingBackend, 'routes_model', return_value=False):
            self.assertIsNone(router.db_for_read(Site))
            self.assertIsNone(router.db_for_write(Site))

    def test_allow_migrate_uses_backend_alias_ownership(self):
        router = BranchAwareRouter()
        # The dummy backend owns 'dummy_*', not 'schema_*'.
        self.assertFalse(router.allow_migrate('dummy_abc', 'netbox_branching', 'branch'))
        self.assertIsNone(router.allow_migrate('schema_branch_abc', 'netbox_branching', 'branch'))

    def test_allow_migrate_defers_to_the_backend(self):
        """
        A backend whose branch is a full copy of main's database needs to permit the
        migrations the default refuses. Prove the router hands the decision over rather
        than making it itself.
        """
        router = BranchAwareRouter()
        with mock.patch.object(DummyBranchingBackend, 'allow_migrate', return_value=None) as m:
            self.assertIsNone(router.allow_migrate('dummy_abc', 'netbox_branching', 'branch'))
        m.assert_called_once_with('dummy_abc', 'netbox_branching', model_name='branch')


class BackendCacheIsolationTestCase(TestCase):
    """
    The module-level instance cache is shared process-wide, so a leaked entry would
    hand a later test the wrong backend. Assert the invalidation hook keeps it clean.
    """

    def test_cache_cleared_on_setting_change(self):
        get_branching_backend()
        self.assertTrue(backends._backends)
        with override_settings(PLUGINS_CONFIG={'netbox_branching': {}}):
            pass
        self.assertFalse(backends._backends)
