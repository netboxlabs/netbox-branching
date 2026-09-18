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
import warnings
from unittest import mock

from dcim.models import Site
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.test import TestCase, override_settings

from netbox_branching import backends
from netbox_branching.backends import (
    PLUGIN_NAME,
    BranchingBackend,
    SchemaBranchingBackend,
    get_branching_backend,
)
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

from .utils import DEFAULT_HOST_CONFIG, plugin_disabled

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
        # Connection parameters visible from inside deprovision(), so a test can assert
        # they are still registered while the backend is tearing the branch down.
        self.params_during_deprovision = None

    def provision(self, branch, user):
        self.calls.append(('provision', branch.pk, user))
        if not branch.backend_id:
            branch.set_backend_id(f'dummy-{branch.pk}')
        self.status_during_provision = Branch.objects.get(pk=branch.pk).status

    def deprovision(self, branch):
        alias = self.get_connection_alias(branch)
        self.params_during_deprovision = self.get_registered_connection_params(alias)
        self.calls.append(('deprovision', branch.pk))
        # Discarding the branch's parameters is the backend's job, not the model's.
        self.invalidate_connection(alias)

    def get_connection_alias(self, branch):
        return self.connection_alias(branch.backend_id)

    def get_connection_config(self, alias, default_config):
        if self.connection_alias_suffix(alias) is None:
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


class TemplateMigrationBackend(DummyBranchingBackend):
    """
    A backend which keeps ``BranchingBackend.apply_migrations()`` and overrides only the
    hook, which is what a backend whose branch is an ordinary database does.
    """
    connection_alias_prefix = 'template_'

    apply_migrations = BranchingBackend.apply_migrations

    def __init__(self):
        super().__init__()
        self.branch_active_in_hook = 'not called'

    def run_migration_plan(self, branch, executor, targets, plan):
        self.calls.append(('run_migration_plan', branch.pk, targets, plan))
        self.branch_active_in_hook = active_branch.get()


class NotABackend:
    pass


class PrefixlessBackend(DummyBranchingBackend):
    """
    A fully-implemented backend whose author simply forgot to declare a
    connection_alias_prefix.
    """
    connection_alias_prefix = None


class UndeclaredLegacyParamBackend(DummyBranchingBackend):
    """
    A backend naming a parameter as legacy without declaring a default for it, so the
    parameter would be readable from the root of the configuration and from nowhere else.
    """
    connection_alias_prefix = 'undeclared_'
    legacy_config_params = ('some_param',)


class ConfigurableBackend(DummyBranchingBackend):
    """
    A backend with its own parameters, one of which it also honours at the root of the
    plugin's configuration for backward compatibility.
    """
    connection_alias_prefix = 'configurable_'
    default_config = {  # noqa: RUF012
        'endpoint': 'https://db.example.com',
        'timeout': 30,
    }
    legacy_config_params = ('endpoint',)


class GetBranchingBackendTestCase(TestCase):

    @override_settings(PLUGINS_CONFIG=DEFAULT_HOST_CONFIG)
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

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {'backend': 'netbox_branching.tests.test_backends.PrefixlessBackend'},
    })
    def test_backend_without_a_connection_alias_prefix_raises(self):
        """
        A backend which forgets to declare a prefix must fail at startup rather than
        inherit one. Silently inheriting the shipped backend's 'schema_' would have it
        claim every schema_* alias left behind by a previous install, and satisfy
        get_connection_alias()'s "must begin with this backend's prefix" rule vacuously.
        """
        with self.assertRaisesRegex(ImproperlyConfigured, 'connection_alias_prefix'):
            get_branching_backend()

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {'backend': 'netbox_branching.tests.test_backends.UndeclaredLegacyParamBackend'},
    })
    def test_backend_with_an_undeclared_legacy_param_raises(self):
        """
        A parameter named in legacy_config_params but missing from default_config could be
        read from the root of the configuration and from nowhere else — there would be no
        backend_config key to migrate it to, and get_config() would reject it outright.
        """
        with self.assertRaisesRegex(ImproperlyConfigured, 'legacy_config_params'):
            get_branching_backend()

    def test_not_required_matches_required_when_plugin_enabled(self):
        self.assertIs(get_branching_backend(required=False), get_branching_backend())

    def test_not_required_returns_none_when_plugin_is_not_installed(self):
        """
        DATABASES and DATABASE_ROUTERS are host configuration and stay wired up when the
        plugin is dropped from PLUGINS, so the shims that read them must be able to ask
        for a backend and be told there isn't one.

        Note what is *not* overridden here: the plugin's PLUGINS_CONFIG block. NetBox never
        clears one, so it outlives the removal, and a guard which tested PLUGINS_CONFIG
        would answer "enabled" in exactly this situation.
        """
        with plugin_disabled():
            self.assertIn(PLUGIN_NAME, settings.PLUGINS_CONFIG, msg="Precondition: config survives")
            self.assertIsNone(get_branching_backend(required=False))

    @override_settings(PLUGINS_CONFIG={})
    def test_missing_plugin_config_raises_when_required(self):
        """
        The strict path has no graceful degradation: without a config block to read,
        get_plugin_config() raises "Plugin netbox_branching is not registered." This is what
        required=False exists to spare the host-configuration shims from.
        """
        with self.assertRaises(ImproperlyConfigured):
            get_branching_backend()

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend': 'nonexistent.module.Backend'}})
    def test_not_required_still_raises_on_a_bad_backend(self):
        """
        Only the plugin's absence is graceful. An enabled plugin naming an unimportable
        backend must fail loudly rather than degrade to no branching at all.
        """
        with self.assertRaises(ImproperlyConfigured):
            get_branching_backend(required=False)


class BackendConfigTestCase(TestCase):
    """
    Backend-specific parameters live under the plugin's ``backend_config``, keeping each
    backend's configuration namespaced from the plugin's own and from every other
    backend's. The three the shipped schema backend predates that parameter with are still
    read from the root of the configuration, deprecated.
    """

    def setUp(self):
        self.backend = ConfigurableBackend()
        self.schema_backend = SchemaBranchingBackend()

    #
    # Resolution
    #

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {}})
    def test_unset_parameter_falls_back_to_the_declared_default(self):
        self.assertEqual(self.backend.get_config('endpoint'), 'https://db.example.com')
        self.assertEqual(self.schema_backend.get_config('schema_prefix'), 'branch_')

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {'backend_config': {'endpoint': 'https://other.example.com'}},
    })
    def test_backend_config_value_is_used(self):
        self.assertEqual(self.backend.get_config('endpoint'), 'https://other.example.com')

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {'backend_config': {'endpoint': 'https://other.example.com'}},
    })
    def test_backend_config_need_not_be_exhaustive(self):
        """
        An operator setting one parameter must not have to restate the rest; resolution is
        per-parameter rather than whole-dict.
        """
        self.assertEqual(self.backend.get_config('timeout'), 30)

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend_config': {'timeout': None}}})
    def test_a_configured_none_is_honoured(self):
        """
        None is a meaningful value for some parameters — the schema backend accepts it for
        provision_workers and falls back to a single worker — so absence has to be
        distinguished from it rather than inferred from falsiness.
        """
        self.assertIsNone(self.backend.get_config('timeout'))

    def test_unknown_parameter_raises(self):
        """
        A backend asking for a parameter it never declared is a bug in the backend, and
        one that would otherwise surface as a silent None somewhere far from its cause.
        """
        with self.assertRaises(KeyError):
            self.backend.get_config('nonexistent')

    @override_settings(PLUGINS_CONFIG={})
    def test_unreadable_configuration_falls_back_to_defaults(self):
        """
        Backend configuration is reached from inside Django's connection-creation path,
        which NetBox's settings module enters while still executing — before there is any
        PLUGINS_CONFIG to read.
        """
        self.assertEqual(self.schema_backend.get_config('main_schema'), 'public')

    #
    # Backward compatibility
    #

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'endpoint': 'https://legacy.example.com'}})
    def test_legacy_root_level_value_is_honoured(self):
        self.assertEqual(self.backend.get_config('endpoint'), 'https://legacy.example.com')

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'schema_prefix': 'legacy_'}})
    def test_the_schema_backend_honours_its_root_level_parameters(self):
        self.assertEqual(self.schema_backend.get_config('schema_prefix'), 'legacy_')
        self.assertEqual(self.schema_backend.get_schema_name('abcd1234'), 'legacy_abcd1234')

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {
        'endpoint': 'https://legacy.example.com',
        'backend_config': {'endpoint': 'https://current.example.com'},
    }})
    def test_backend_config_wins_over_a_root_level_value(self):
        self.assertEqual(self.backend.get_config('endpoint'), 'https://current.example.com')

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'timeout': 5}})
    def test_a_root_level_value_is_ignored_for_a_parameter_with_no_legacy_spelling(self):
        """
        Only the parameters a backend declares as legacy are read from the root. Reading
        every parameter from there would have a backend silently capture an unrelated
        plugin parameter of the same name.
        """
        self.assertEqual(self.backend.get_config('timeout'), 30)

    #
    # Deprecation warnings
    #

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'schema_prefix': 'legacy_'}})
    def test_root_level_parameter_warns(self):
        with self.assertWarnsRegex(FutureWarning, "'schema_prefix'"):
            self.schema_backend.check_deprecated_config()

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {
        'schema_prefix': 'legacy_',
        'backend_config': {'schema_prefix': 'current_'},
    }})
    def test_a_shadowed_root_level_parameter_says_which_value_wins(self):
        with self.assertWarnsRegex(FutureWarning, 'takes effect'):
            self.schema_backend.check_deprecated_config()

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {
        'backend_config': {'schema_prefix': 'current_', 'main_schema': 'nb', 'provision_workers': 2},
    }})
    def test_backend_config_alone_does_not_warn(self):
        self.assertEqual(self._warnings(self.schema_backend), [])

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'job_timeout': 60, 'max_branches': 5}})
    def test_unrelated_root_level_parameters_do_not_warn(self):
        self.assertEqual(self._warnings(self.schema_backend), [])

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'schema_prefix': 'legacy_'}})
    def test_a_backend_warns_only_about_its_own_parameters(self):
        """
        ConfigurableBackend has no schema_prefix, so an installation which switched
        backends without tidying its configuration must not be told to move a parameter
        the configured backend does not read.
        """
        self.assertEqual(self._warnings(self.backend), [])

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {
        'main_schema': 'nb',
        'schema_prefix': 'legacy_',
    }})
    def test_every_misplaced_parameter_is_reported(self):
        """
        One warning per parameter, so an operator moving them does not have to restart
        NetBox once per parameter to discover the next one.
        """
        warned = self._warnings(self.schema_backend)
        self.assertEqual(len(warned), 2)
        for name in ('main_schema', 'schema_prefix'):
            matching = [msg for msg in warned if f"'{name}'" in msg]
            self.assertEqual(len(matching), 1, msg=f"Expected one warning naming {name}: {warned}")

    @staticmethod
    def _warnings(backend):
        """
        Return the warning messages check_deprecated_config() emits, as strings.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            backend.check_deprecated_config()
        return [str(w.message) for w in caught]


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

    def test_schema_name_requires_an_identifier(self):
        """
        A branch has no identifier until it is provisioned. Interpolating a missing one
        would name a schema which does not exist, and a connection whose search_path
        names a nonexistent schema falls through to main — every query made "within" the
        branch would silently read and write main instead.
        """
        with self.assertRaises(ValueError):
            self.backend.get_schema_name(None)

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend_config': {'schema_prefix': 'dummy_'}}})
    def test_schema_name_applies_the_configured_prefix(self):
        self.assertEqual(self.backend.get_schema_name('abcd1234'), 'dummy_abcd1234')

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

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend_config': {'schema_prefix': 'x' * 100}}})
    def test_overlong_prefix_leaves_the_branch_retryable(self):
        """
        The name is checked before the identifier is committed. An identifier is immutable
        once assigned, so a branch which had been given one would re-derive the same
        over-long schema name on every retry and fail identically, even once the operator
        had shortened schema_prefix — leaving no way out but deleting the branch.
        """
        branch = Branch(name='Long Prefix Branch')
        branch.save(provision=False)

        with self.assertRaisesRegex(ImproperlyConfigured, 'exceeds'):
            self.backend.provision(branch, user=None)

        # No identifier was committed, so a retry under a corrected schema_prefix mints a
        # fresh one instead of inheriting the doomed one.
        branch.refresh_from_db()
        self.assertIsNone(branch.backend_id, msg="An unusable identifier was committed")

    def test_detail_fields_report_no_schema_once_deprovisioned(self):
        """
        The schema name is derived from backend_id, which archive() deliberately retains after
        dropping the schema. The detail page must not present the name of a schema that no
        longer exists as though it were live.
        """
        provisioned = Branch(name='Live', backend_id='live1234', provisioned=True)
        ((label, value),) = self.backend.get_detail_fields(provisioned)
        self.assertEqual(str(label), 'Database schema')
        self.assertEqual(value, self.backend.get_schema_name(provisioned.backend_id))

        archived = Branch(
            name='Archived', backend_id='arch1234', provisioned=False,
            status=BranchStatusChoices.ARCHIVED,
        )
        ((_label, value),) = self.backend.get_detail_fields(archived)
        self.assertIsNone(value, msg="A dropped schema was reported as the branch's own")


@override_settings(PLUGINS_CONFIG=DEFAULT_HOST_CONFIG)
class BaseBackendDefaultsTestCase(TestCase):
    """
    Behaviour ``BranchingBackend`` supplies to every backend, exercised through a backend
    which does not override any of it. These used to be written once per backend, which
    is how the shipped one and the out-of-tree one came to hold copies of the same checks.
    """

    def setUp(self):
        self.backend = DummyBranchingBackend()
        self.branch = Branch(name='Base Defaults', backend_id='basedflt')

    #
    # Host configuration
    #

    def test_host_checks_apply_to_a_backend_which_adds_none_of_its_own(self):
        self.backend.validate_configuration()  # must not raise

    def test_plain_databases_dict_raises_for_any_backend(self):
        # Patched directly rather than via override_settings: overriding DATABASES makes
        # Django tear down and rebuild every connection, which would break the transaction
        # this test runs inside.
        with (
            mock.patch.object(settings, 'DATABASES', {'default': {}}),
            self.assertRaises(ImproperlyConfigured),
        ):
            self.backend.validate_configuration()

    @override_settings(DATABASE_ROUTERS=[])
    def test_missing_router_raises_for_any_backend(self):
        with self.assertRaises(ImproperlyConfigured):
            self.backend.validate_configuration()

    #
    # Backend parameters
    #

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend_config': {'endpoint': 'https://x'}}})
    def test_an_unrecognized_parameter_raises(self):
        """
        get_config() guards a name the backend's own source asks for and has not declared.
        This is the other direction: an operator's typo, which would otherwise resolve to
        the default silently and permanently.
        """
        backend = ConfigurableBackend()
        backend.validate_configuration()  # 'endpoint' is one of its parameters

        with self.assertRaises(ImproperlyConfigured) as ctx:
            # ... but not one of this backend's, which declares none at all.
            self.backend.validate_configuration()
        self.assertIn('endpoint', str(ctx.exception))

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend_config': {'timeuot': 5}}})
    def test_the_message_names_the_typo_and_what_was_expected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            ConfigurableBackend().validate_configuration()

        message = str(ctx.exception)
        self.assertIn('timeuot', message)
        self.assertIn('ConfigurableBackend', message)
        # The parameters it does accept, so the fix does not need the source.
        self.assertIn('endpoint', message)
        self.assertIn('timeout', message)

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'exempt_models': ['dcim.site']}})
    def test_the_plugins_own_parameters_are_not_the_backends(self):
        """
        Only `backend_config` is scanned. The plugin's own settings sit at the root and are
        no business of the backend's.
        """
        self.backend.validate_configuration()  # must not raise

    #
    # Alias construction
    #

    def test_connection_alias_round_trips(self):
        alias = self.backend.connection_alias('basedflt')
        self.assertEqual(alias, 'dummy_basedflt')
        self.assertEqual(self.backend.connection_alias_suffix(alias), 'basedflt')

    def test_connection_alias_requires_an_identifier(self):
        """
        Invariant 4: nothing may address a branch's dataset before provisioning names it.
        An alias of 'dummy_None' would instead be shared by every unprovisioned branch.
        """
        for missing in (None, ''):
            with self.assertRaises(ValueError):
                self.backend.connection_alias(missing)

    def test_connection_alias_suffix_declines_what_is_not_ours(self):
        self.assertIsNone(self.backend.connection_alias_suffix('default'))
        self.assertIsNone(self.backend.connection_alias_suffix('schema_branch_abc123'))
        self.assertIsNone(self.backend.connection_alias_suffix(None))
        # A bare prefix carries no identifier, so it addresses no branch.
        self.assertIsNone(self.backend.connection_alias_suffix('dummy_'))

    #
    # Dataset composition
    #

    def test_objectchange_table_is_netboxs_changelog(self):
        from core.models import ObjectChange

        self.assertEqual(self.backend.objectchange_table, ObjectChange._meta.db_table)

    def test_branch_tables_cover_the_branchable_models_and_the_changelog(self):
        tables = self.backend.get_branch_tables()
        self.assertIn(Site._meta.db_table, tables)
        self.assertIn(self.backend.objectchange_table, tables)
        # The plugin's own models are not part of a branch's dataset.
        self.assertNotIn(Branch._meta.db_table, tables)

    #
    # Migrations
    #

    def test_get_pending_migrations_need_not_be_implemented(self):
        """
        It has a working default built on Django's migration executor, so a backend whose
        branch is an ordinary Django database inherits it. Its behaviour against a real
        provisioned branch is covered by test_branches.py.
        """
        self.assertNotIn('get_pending_migrations', BranchingBackend.__abstractmethods__)


class ApplyMigrationsTemplateTestCase(TestCase):
    """
    ``apply_migrations()`` is a template method: what it does around the plan is contract,
    and only the application of the plan itself is the backend's business. The end-to-end
    path is covered by ``test_upgrade.py``; these pin the wiring, which is what a backend
    inherits by not overriding it.
    """

    def setUp(self):
        self.backend = TemplateMigrationBackend()
        self.branch = Branch(name='Template Branch', backend_id='template')
        self.branch.save(provision=False)

        self.executor = mock.Mock()
        self.targets = [('dcim', '0500_leaf')]
        self.executor.loader.graph.leaf_nodes.return_value = self.targets

    def _run(self, plan, progress_callback=None):
        """Run apply_migrations() with the executor and connection handler stubbed out."""
        self.executor.migration_plan.return_value = plan
        with (
            mock.patch.object(backends.base, 'connections', mock.MagicMock()),
            mock.patch.object(backends.base, 'MigrationExecutor', return_value=self.executor) as ctor,
        ):
            self.backend.apply_migrations(self.branch, progress_callback=progress_callback)
        return ctor

    def test_an_empty_plan_short_circuits_before_the_hook(self):
        with self.assertLogs('netbox_branching.branch.migrate', level='INFO') as logs:
            self._run(plan=[])

        self.assertNotIn(
            'run_migration_plan',
            [call[0] for call in self.backend.calls],
            msg="The hook ran for a branch with nothing to apply",
        )
        self.assertTrue(any('no migrations to apply' in line for line in logs.output))

    def test_the_hook_receives_the_executor_targets_and_plan(self):
        plan = [(mock.Mock(), False)]
        self._run(plan=plan)

        self.assertIn(
            ('run_migration_plan', self.branch.pk, self.targets, plan),
            self.backend.calls,
        )

    def test_the_branch_is_active_while_the_plan_runs(self):
        """
        Not optional: ORM queries inside a RunPython migration otherwise fall through the
        router to main, which may already have been migrated past the columns the branch's
        pending migration depends on.
        """
        self.assertIsNone(active_branch.get(), msg="Precondition: no branch active")

        self._run(plan=[(mock.Mock(), False)])

        self.assertEqual(self.backend.branch_active_in_hook, self.branch)
        self.assertIsNone(active_branch.get(), msg="The branch stayed active after migrating")

    def test_the_progress_callback_reaches_the_executor(self):
        callback = mock.Mock()
        ctor = self._run(plan=[(mock.Mock(), False)], progress_callback=callback)

        self.assertEqual(ctor.call_args.kwargs['progress_callback'], callback)

    def test_the_default_hook_is_an_ordinary_executor_run(self):
        """
        What a backend gets by overriding neither: the plain migrate() that is correct when
        a branch is a self-contained database.
        """
        backend = DummyBranchingBackend()
        plan = [(mock.Mock(), False)]

        BranchingBackend.run_migration_plan(backend, self.branch, self.executor, self.targets, plan)

        self.executor.migrate.assert_called_once_with(self.targets)


@override_settings(PLUGINS_CONFIG=DEFAULT_HOST_CONFIG)
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
        self.assertEqual(alias, f'schema_{self.backend.get_schema_name(self.branch.backend_id)}')
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


@override_settings(PLUGINS_CONFIG=DEFAULT_HOST_CONFIG)
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

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend_config': {'provision_workers': 'four'}}})
    def test_non_integer_provision_workers_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            self.backend.validate_configuration()

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend_config': {'provision_workers': 0}}})
    def test_zero_provision_workers_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            self.backend.validate_configuration()

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'backend_config': {'provision_workers': None}}})
    def test_none_provision_workers_is_permitted(self):
        self.backend.validate_configuration()  # must not raise


@override_settings(PLUGINS_CONFIG=DEFAULT_HOST_CONFIG)
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

    def test_invalidate_connection_discards_params_and_wrapper(self):
        """
        A backend whose endpoint moves has to drop the DatabaseWrapper, not just close it:
        Django reads get_connection_config() once, when the connection is created, and
        keeps that settings_dict for the wrapper's life.
        """
        backend = SchemaBranchingBackend()
        alias = 'schema_branch_invalidate'
        backend.register_connection_params(alias, {'HOST': 'replica.example.com'})
        # Touching the alias is what makes Django build and retain the wrapper.
        self.assertIsNotNone(connections[alias])
        self.assertTrue(hasattr(connections._connections, alias))

        backend.invalidate_connection(alias)

        self.assertFalse(hasattr(connections._connections, alias))
        self.assertIsNone(backend.get_registered_connection_params(alias))

    def test_invalidate_connection_is_safe_for_an_unopened_alias(self):
        # Deprovisioning a branch this thread never addressed must not build a connection
        # to it on the way out.
        backend = SchemaBranchingBackend()
        alias = 'schema_branch_never_opened'

        backend.invalidate_connection(alias)

        self.assertFalse(hasattr(connections._connections, alias))


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

    def _mark_provisioned(self):
        """
        Stand in for the provision these tests do not run. Branch.migrate() refuses a
        branch with no dataset behind it, so a test reaching the backend at all has to
        record the state a successful provision would have left.
        """
        self.branch.status = BranchStatusChoices.READY
        self.branch.provisioned = True
        self.branch.save(update_merge_sync_fields=True)

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

    def test_deprovision_evicts_registered_connection_params(self):
        """
        register_connection_params() is how a backend carries per-branch endpoints and
        credentials from get_connection_alias() (which may query) to
        get_connection_config() (which must not). Nothing evicted those entries, so a
        long-lived RQ worker accumulated one per branch it had ever addressed and held a
        dead branch's credentials for the life of the process.

        The eviction is the backend's own, via invalidate_connection(); this asserts the
        outcome, which the contract requires of every backend, not the route to it.
        """
        alias = self.branch.connection_name
        self.backend.register_connection_params(alias, {'HOST': 'branch.example.com'})
        self.assertIsNotNone(self.backend.get_registered_connection_params(alias))

        self.branch.deprovision()

        self.assertIsNone(
            self.backend.get_registered_connection_params(alias),
            msg="Connection params survived the destruction of the branch they addressed",
        )

    def test_deprovision_does_not_ask_the_backend_to_name_the_branch(self):
        """
        Reading connection_name was how the model reached the branch's parameters, and it
        did so at the one moment the dataset is expected to be gone. A backend which
        reaches its dataset in order to name it raised there — inside a finally, so it
        masked whatever the backend had raised first. See #654.
        """
        with (
            mock.patch.object(
                DummyBranchingBackend,
                'get_connection_alias',
                side_effect=AssertionError('Branch.deprovision() resolved a connection alias'),
            ),
            # The backend's own deprovision() may legitimately name the branch; this is
            # about what the model does around it.
            mock.patch.object(DummyBranchingBackend, 'deprovision'),
        ):
            self.branch.deprovision()

        self.branch.refresh_from_db()
        self.assertFalse(self.branch.provisioned)

    def test_a_branch_whose_alias_cannot_be_resolved_can_still_be_deleted(self):
        """
        The user-visible consequence: delete() wraps the row delete and the deprovision in
        one transaction, so anything raised during teardown rolled the deletion back.
        Branches whose dataset was already gone — archived ones, and failed provisions —
        could not be deleted at all.
        """
        pk = self.branch.pk
        with (
            mock.patch.object(
                DummyBranchingBackend,
                'get_connection_alias',
                side_effect=RuntimeError('no dataset behind this branch'),
            ),
            mock.patch.object(DummyBranchingBackend, 'deprovision'),
        ):
            self.branch.delete()

        self.assertFalse(Branch.objects.filter(pk=pk).exists())

    def test_provision_fails_when_the_backend_assigns_no_identifier(self):
        """
        Assigning the branch's identifier is part of provision()'s contract. A backend which
        returns without doing so leaves nothing to address its dataset by, and the CHECK
        constraint would then reject the READY transition — which happens after the dataset
        exists and outside the cleanup path, stranding the branch in PROVISIONING with an
        orphaned dataset until the stuck-branch watchdog finds it. See #618.
        """
        branch = Branch(name='Identifierless Branch')
        branch.save(provision=False)

        with (
            mock.patch.object(DummyBranchingBackend, 'provision', return_value=None),
            self.assertRaisesRegex(ImproperlyConfigured, 'without assigning a backend ID'),
        ):
            branch.provision(user=None)

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.FAILED)
        self.assertFalse(branch.provisioned)

    def test_deprovision_evicts_connection_params_only_once_the_backend_is_done(self):
        """
        The eviction above must not run first. A backend which reaches the branch over a
        connection of its own needs those parameters to tear it down — which is the
        second reason the obligation sits with the backend: it is the only party that
        knows when it is finished with them.
        """
        params = {'HOST': 'branch.example.com'}
        self.backend.register_connection_params(self.branch.connection_name, params)

        self.branch.deprovision()

        self.assertEqual(
            self.backend.params_during_deprovision,
            params,
            msg="Connection params were evicted before the backend could tear the branch down",
        )

    def test_pending_migrations_delegates(self):
        # Only a provisioned branch can have migrations outstanding; while the branch is
        # still NEW, Branch.pending_migrations short-circuits to [] without consulting the
        # backend at all (that short-circuit is pinned by BranchTestCase).
        self._mark_provisioned()

        self.assertEqual(self.branch.pending_migrations, [('dcim', '9999_dummy')])
        self.assertIn(('get_pending_migrations', self.branch.pk), self.backend.calls)

    def test_migrate_delegates_and_preserves_lifecycle(self):
        self._mark_provisioned()
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
        self._mark_provisioned()
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
