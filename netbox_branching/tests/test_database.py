"""
Unit tests for the routing primitives that make branch isolation work:

  * DynamicSchemaDict — the dict subclass installed as DATABASES that fabricates
    a per-branch config (with the right search_path) on every "schema_*" lookup
  * BranchAwareRouter — the database router that returns the active branch's
    connection alias for branchable models
  * close_old_branch_connections / track_branch_connection — tracking and
    cleanup for dynamically-created branch connections (issue #358)

These are exercised indirectly by every branching test, but failures there
surface as confusing routing errors. The tests here pin down the contracts of
each primitive in isolation so regressions can be diagnosed quickly.
"""
from dcim.models import Site
from django.test import TestCase, override_settings

from netbox_branching.backends import SchemaBranchingBackend
from netbox_branching.contextvars import active_branch
from netbox_branching.database import BranchAwareRouter
from netbox_branching.models import Branch
from netbox_branching.utilities import (
    DynamicSchemaDict,
    _get_tracked_branch_aliases,
    close_old_branch_connections,
    track_branch_connection,
)

from .utils import DEFAULT_HOST_CONFIG, plugin_disabled


class PickyBackend(SchemaBranchingBackend):
    """
    A backend which owns every "schema_*" alias but declines to configure one of them,
    standing in for any backend whose ownership test is broader than its config method.
    """
    def get_connection_config(self, alias, default_config):
        if alias.endswith('_refused'):
            return None
        return super().get_connection_config(alias, default_config)


class CountingBackend(SchemaBranchingBackend):
    """
    A backend which counts get_connection_alias() calls, standing in for any backend
    which does real work there -- the contract permits it to query the database.
    """
    alias_calls = 0

    def get_connection_alias(self, branch):
        CountingBackend.alias_calls += 1
        return super().get_connection_alias(branch)


@override_settings(PLUGINS_CONFIG=DEFAULT_HOST_CONFIG)
class DynamicSchemaDictTestCase(TestCase):
    """
    DynamicSchemaDict fabricates a per-branch DATABASE config for any
    "schema_*" key, and claims membership for those keys so Django's
    ConnectionHandler will dispatch them through __getitem__.
    """

    def _make(self):
        return DynamicSchemaDict({
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': 'netbox',
                'OPTIONS': {'connect_timeout': 10},
            }
        })

    def test_schema_key_returns_dynamic_config(self):
        config = self._make()['schema_branch_abc123']
        self.assertEqual(config['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(config['NAME'], 'netbox')
        self.assertEqual(config['OPTIONS']['connect_timeout'], 10)
        self.assertIn('search_path=branch_abc123', config['OPTIONS']['options'])

    def test_default_key_returns_underlying_config(self):
        databases = self._make()
        self.assertEqual(databases['default']['NAME'], 'netbox')

    def test_contains_lies_about_schema_keys(self):
        """
        ConnectionHandler uses `alias in DATABASES` to decide whether to call
        __getitem__. DynamicSchemaDict must claim membership for any schema_*
        key, even ones never seen before.
        """
        databases = self._make()
        self.assertIn('schema_branch_unseen', databases)
        self.assertIn('default', databases)
        self.assertNotIn('something_else', databases)

    def test_owned_but_unconfigurable_alias_is_not_claimed(self):
        """
        __contains__ and __getitem__ must give the same answer. Django's ConnectionHandler
        tests membership and then indexes, so an alias claimed here but refused on lookup
        raises a bare KeyError out of create_connection() instead of the
        ConnectionDoesNotExist it raises for an alias reported unknown.

        The bare prefix is the shipped backend's reachable case: it satisfies
        owns_connection_alias()'s startswith test, but leaves get_connection_config() with
        an empty schema name, which it declines to build a search_path from.
        """
        databases = self._make()
        self.assertNotIn('schema_', databases)
        with self.assertRaises(KeyError):
            databases['schema_']

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {'backend': 'netbox_branching.tests.test_database.PickyBackend'},
    })
    def test_alias_a_backend_owns_but_refuses_is_not_claimed(self):
        """
        The general case: a backend whose ownership test is broader than the set of aliases
        it can actually configure. Both lookups must agree that such an alias is absent.
        """
        databases = self._make()
        self.assertIn('schema_branch_ok', databases)
        self.assertEqual(databases['schema_branch_ok']['NAME'], 'netbox')

        self.assertNotIn('schema_branch_refused', databases)
        with self.assertRaises(KeyError):
            databases['schema_branch_refused']

    def test_lookup_registers_alias_for_cleanup_tracking(self):
        """
        Branch aliases are not in DATABASES.keys(), so close_old_connections()
        would miss them. DynamicSchemaDict registers each accessed alias so
        close_old_branch_connections() can find them later.
        """
        alias = 'schema_branch_track_test'
        _get_tracked_branch_aliases().discard(alias)
        self._make()[alias]
        try:
            self.assertIn(alias, _get_tracked_branch_aliases())
        finally:
            _get_tracked_branch_aliases().discard(alias)

    # Plugin disabled ----------------------------------------------------------
    #
    # DATABASES is host configuration: it stays wrapped in a DynamicSchemaDict when the
    # operator drops netbox_branching from PLUGINS (to troubleshoot, say). With no plugin
    # loaded, the wrapper must behave as the plain dict it subclasses rather than raising
    # ImproperlyConfigured on NetBox's very first database lookup.
    #
    # The operator's PLUGINS_CONFIG block survives that removal, so these use
    # plugin_disabled() rather than emptying PLUGINS_CONFIG — the latter describes a state
    # NetBox cannot produce, and passes against a guard which never fires in production.

    def test_default_key_works_with_plugin_disabled(self):
        with plugin_disabled():
            self.assertEqual(self._make()['default']['NAME'], 'netbox')

    def test_schema_key_is_not_claimed_with_plugin_disabled(self):
        with plugin_disabled():
            databases = self._make()
            self.assertNotIn('schema_branch_abc123', databases)
            with self.assertRaises(KeyError):
                databases['schema_branch_abc123']


@override_settings(PLUGINS_CONFIG=DEFAULT_HOST_CONFIG)
class BranchAwareRouterTestCase(TestCase):
    """
    BranchAwareRouter consults the active_branch ContextVar to decide whether
    queries should be routed to a branch schema. The router is purely a Python
    object — it can be unit-tested without provisioning a real schema, as long
    as the Branch has a backend ID (assigned by the backend during provisioning,
    so pinned explicitly here).
    """

    def setUp(self):
        self.router = BranchAwareRouter()
        self.branch = Branch(name='Router Test Branch', backend_id='routertst')
        # Built from the backend's own derivation rather than from
        # Branch.connection_name, which is the very call the router makes.
        self.branch_alias = f'schema_{self.branch.backend.get_schema_name(self.branch.backend_id)}'

    def _activate(self, branch):
        """Set active_branch and register a cleanup that restores it."""
        token = active_branch.set(branch)
        self.addCleanup(active_branch.reset, token)

    # db_for_read / db_for_write -----------------------------------------------

    def test_db_for_read_returns_none_without_active_branch(self):
        self.assertIsNone(self.router.db_for_read(Site))

    def test_db_for_read_returns_branch_alias_when_active(self):
        self._activate(self.branch)
        self.assertEqual(self.router.db_for_read(Site), self.branch_alias)

    def test_db_for_write_mirrors_db_for_read(self):
        self._activate(self.branch)
        self.assertEqual(self.router.db_for_write(Site), self.branch_alias)

    def test_db_for_read_returns_none_for_non_branchable_model(self):
        """
        Models without the 'branching' feature (e.g. the plugin's own Branch
        model) must never be routed to a branch schema, even when one is active.
        """
        self._activate(self.branch)
        self.assertIsNone(self.router.db_for_read(Branch))
        self.assertIsNone(self.router.db_for_write(Branch))

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {'exempt_models': ['dcim.site']},
    })
    def test_exempt_model_routes_to_main_even_when_branch_active(self):
        """
        exempt_models is read dynamically by supports_branching() on every
        query, so adding a model to the list must immediately stop the router
        from sending it to the branch — even for a branch that was provisioned
        before the model was exempted. Without this guarantee, exempting a
        model in a running NetBox process wouldn't take effect until restart.
        """
        self._activate(self.branch)
        self.assertIsNone(self.router.db_for_write(Site))
        self.assertIsNone(self.router.db_for_read(Site))

    def test_object_change_always_routes_to_active_branch(self):
        """
        The changelog (core.ObjectChange) is special-cased in db_for_read:
        when a branch is active it must be read from the branch schema, even
        though ObjectChange is not branchable in the supports_branching sense.
        """
        from core.models import ObjectChange
        self.assertIsNone(self.router.db_for_read(ObjectChange))
        self._activate(self.branch)
        self.assertEqual(self.router.db_for_read(ObjectChange), self.branch_alias)

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {'backend': 'netbox_branching.tests.test_database.CountingBackend'},
    })
    def test_alias_is_resolved_once_per_branch_instance(self):
        """
        The router must reach the backend through Branch.connection_name, whose
        cached_property is the funnel the contract describes: get_connection_alias() is
        allowed to query the database, so calling it per ORM query would add a query to
        every query for a backend which resolves its endpoint lazily.
        """
        from core.models import ObjectChange

        branch = Branch(name='Counting Branch', backend_id='countbr')
        self._activate(branch)
        CountingBackend.alias_calls = 0

        aliases = {
            self.router.db_for_read(Site),
            self.router.db_for_write(Site),
            self.router.db_for_read(ObjectChange),
        }

        self.assertEqual(aliases, {f'schema_branch_{branch.backend_id}'})
        self.assertEqual(CountingBackend.alias_calls, 1)

    # allow_relation -----------------------------------------------------------

    def test_allow_relation_always_true(self):
        """FKs between branch schema and main schema must be permitted."""
        site_a = Site(name='A')
        site_b = Site(name='B')
        self.assertTrue(self.router.allow_relation(site_a, site_b))

    # allow_migrate ------------------------------------------------------------

    def test_allow_migrate_returns_none_for_non_branch_db(self):
        self.assertIsNone(self.router.allow_migrate('default', 'dcim', 'site'))

    def test_allow_migrate_disallows_plugin_models_in_branches(self):
        """The plugin's own tables must live in main, never in a branch schema."""
        self.assertFalse(
            self.router.allow_migrate('schema_branch_xxx', 'netbox_branching', 'branch')
        )

    def test_allow_migrate_allows_object_change(self):
        """core.ObjectChange is replicated to every branch schema."""
        self.assertTrue(
            self.router.allow_migrate('schema_branch_xxx', 'core', 'objectchange')
        )

    def test_allow_migrate_disallows_non_branchable_models(self):
        """auth.User has no 'branching' feature, so it must stay in main only."""
        self.assertFalse(
            self.router.allow_migrate('schema_branch_xxx', 'auth', 'user')
        )

    def test_allow_migrate_has_no_opinion_with_plugin_disabled(self):
        """
        DATABASE_ROUTERS is host configuration that outlives PLUGINS, and Django consults
        allow_migrate() for every model on every `migrate` — so this is the one router
        method reachable with the plugin unloaded. It must abstain, not raise.
        """
        with plugin_disabled():
            self.assertIsNone(self.router.allow_migrate('default', 'dcim', 'site'))
            self.assertIsNone(self.router.allow_migrate('schema_branch_xxx', 'dcim', 'site'))


class BranchConnectionTrackingTestCase(TestCase):
    """
    track_branch_connection() records aliases for close_old_branch_connections()
    to clean up later. Integration with real connections is covered by
    test_connection_lifecycle.py — this just pins the tracker contract.
    """

    def test_track_branch_connection_adds_alias(self):
        alias = 'schema_branch_tracker_unit'
        _get_tracked_branch_aliases().discard(alias)
        track_branch_connection(alias)
        try:
            self.assertIn(alias, _get_tracked_branch_aliases())
        finally:
            _get_tracked_branch_aliases().discard(alias)

    def test_invalidate_connection_untracks_the_alias(self):
        """
        A tracked alias outlives the wrapper invalidate_connection() removes, and
        close_old_branch_connections() reaches every tracked alias through
        connections[alias] -- which builds a new wrapper on demand. Without untracking,
        the next request in this thread would undo the invalidation.
        """
        alias = 'schema_branch_invalidate_unit'
        track_branch_connection(alias)
        self.addCleanup(_get_tracked_branch_aliases().discard, alias)

        SchemaBranchingBackend().invalidate_connection(alias)

        self.assertNotIn(alias, _get_tracked_branch_aliases())

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {'backend': 'netbox_branching.tests.test_database.PickyBackend'},
    })
    def test_close_old_branch_connections_prunes_an_alias_which_no_longer_resolves(self):
        """
        A deprovisioned branch leaves its alias tracked in every thread which touched it
        (invalidate_connection() can only untrack its own). Those threads must not raise
        ConnectionDoesNotExist out of a request_started handler -- which would break
        every subsequent request they serve -- but drop the alias instead.
        """
        alias = 'schema_branch_sweep_refused'
        saved = set(_get_tracked_branch_aliases())
        _get_tracked_branch_aliases().clear()
        track_branch_connection(alias)
        try:
            close_old_branch_connections()  # must not raise
            self.assertNotIn(alias, _get_tracked_branch_aliases())
        finally:
            _get_tracked_branch_aliases().clear()
            _get_tracked_branch_aliases().update(saved)

    def test_close_old_branch_connections_no_error_when_tracker_empty(self):
        """
        close_old_branch_connections runs on every request via signal hookups,
        so it must be safe to call when nothing has been tracked yet. The set
        is module-global, so save/restore around the test.
        """
        saved = set(_get_tracked_branch_aliases())
        _get_tracked_branch_aliases().clear()
        try:
            close_old_branch_connections()  # must not raise
        finally:
            _get_tracked_branch_aliases().clear()
            _get_tracked_branch_aliases().update(saved)
