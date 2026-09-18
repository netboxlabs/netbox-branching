import logging
import warnings
from abc import ABC, abstractmethod

from asgiref.local import Local
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from netbox.plugins import get_plugin_config

from netbox_branching.constants import MIGRATE_LOGGER, PLUGIN_NAME, PROVISION_LOGGER
from netbox_branching.utilities import (
    DynamicSchemaDict,
    activate_branch,
    get_tables_to_replicate,
    supports_branching,
    untrack_branch_connection,
)

__all__ = (
    'PLUGIN_NAME',
    'BranchingBackend',
)

logger = logging.getLogger('netbox_branching.backends')

# The plugin configuration parameter holding backend-specific configuration.
BACKEND_CONFIG_PARAM = 'backend_config'

# Process-local registry of per-branch connection parameters, keyed by connection
# alias. Mirrors the _branch_connections_tracker pattern in utilities.py:
# thread_critical=False so the mapping is shared by coroutines running in the same
# thread. Entries are written by get_connection_alias() (which has a Branch row in
# hand) and read by get_connection_config() (which must not touch the database).
_connection_params_registry = Local(thread_critical=False)


class BranchingBackend(ABC):
    """
    Abstract base class for branching backends.

    A branching backend owns the *mechanism* of branch isolation: how an isolated
    copy of the branchable dataset is created and destroyed, how database
    connections addressing it are named and configured, and how outstanding
    Django migrations are applied to it. Everything above that — change tracking,
    conflict detection, merge strategies, branch status transitions, signals and
    events — is backend-agnostic and lives in the ``Branch`` model.

    Configuration
    -------------
    A backend declares its own parameters and their defaults in ``default_config`` and
    reads them with ``get_config()``. Operators set them under the plugin's
    ``backend_config`` parameter, which keeps a backend's configuration namespaced from
    the plugin's own and from every other backend's:

    .. code-block:: python

        PLUGINS_CONFIG = {
            'netbox_branching': {
                'backend': 'my_plugin.backends.MyBranchingBackend',
                'backend_config': {
                    'cluster_endpoint': 'https://db.example.com',
                },
            }
        }

    Invariants
    ----------
    These are requirements of the surrounding machinery, not of any particular
    storage mechanism. A backend that violates one of them will appear to work
    until a branch is merged.

    1. **Global primary key allocation.** Merge and revert replay each
       ``ObjectChange.changed_object_id`` verbatim against main, so the primary
       key of an object created within a branch must not collide with a primary
       key allocated in main or in a sibling branch.

    2. **Empty changelog on a fresh branch.** ``Branch.get_unmerged_changes()``
       treats every ``core.ObjectChange`` row visible on the branch connection as
       an unmerged branch change. A backend that copies main wholesale must
       therefore truncate that table as part of provisioning; otherwise the first
       merge will replay main's entire change history.

    3. **Exempt-model visibility.** Models which do not support branching
       (``auth.User``, ``contenttypes``, ``core.*``, the plugin's own models) are
       routed to main by ``routes_model()``, but a join issued on the branch
       connection resolves against whatever *that* connection can see. A backend
       whose branch holds a point-in-time copy of those tables accepts display
       staleness there.

    4. **Branch identity is assigned at provisioning time.** A ``Branch`` row
       exists in status ``NEW`` with ``backend_id`` unset; ``provision()`` is what
       gives the branch an identifier. Nothing may address the branch's dataset
       before then.
    """

    # Prefix identifying connection aliases owned by this backend. Every backend must
    # declare its own, and must not reuse another's: owns_connection_alias() claims every
    # alias carrying it, so two backends sharing a prefix would each claim the other's
    # aliases — including ones left behind by a previous install. Deliberately has no
    # usable default, so that omitting it fails loudly at startup (get_branching_backend()
    # rejects a backend which has not set it) rather than silently inheriting someone
    # else's namespace.
    connection_alias_prefix = None

    # This backend's own configuration parameters, mapped to their default values.
    # Operators set them under the plugin's `backend_config` parameter, and the backend
    # reads them through get_config(), which falls back to the default declared here. A
    # name absent from this mapping is not a parameter of this backend at all, and
    # get_config() rejects it rather than inventing a value for it — so a typo in the
    # backend's own source fails loudly instead of silently reading None.
    default_config = {}  # noqa: RUF012

    # Parameters this backend read from the *root* of the plugin's configuration before
    # `backend_config` existed, and which are still honoured there for backward
    # compatibility. Every name listed must also appear in default_config. A root-level
    # value loses to one set under `backend_config`, and either way draws a FutureWarning
    # at startup; see check_deprecated_config(). A backend with no such history — which
    # is every backend but the one shipped here — leaves this empty.
    legacy_config_params = ()

    #
    # Logging
    #

    @property
    def provision_logger(self):
        """
        The logger branch provisioning and deprovisioning report into.

        Named here rather than left to each backend because the channel is part of the
        contract: an operator filtering on it expects to see every backend's provisioning,
        and an out-of-tree backend should not have to recover the string by reading the
        shipped one.
        """
        return logging.getLogger(PROVISION_LOGGER)

    @property
    def migrate_logger(self):
        """
        The logger branch migration reports into. See ``provision_logger``.
        """
        return logging.getLogger(MIGRATE_LOGGER)

    #
    # Branch dataset
    #

    @property
    def objectchange_table(self):
        """
        The name of the table holding NetBox's change log.

        Every backend needs it — it is the table invariant 2 is about — and the import is
        deferred rather than made at module scope because resolving a backend can happen
        while a database connection is being created, potentially before the app registry
        is fully populated.
        """
        from core.models import ObjectChange

        return ObjectChange._meta.db_table

    def get_branch_tables(self):
        """
        Return the names of the tables which make up a branch's dataset: every branchable
        model's table, plus the change log that invariant 2 requires each branch to carry
        its own (initially empty) copy of.

        A backend is free to hold more than this — one whose branch is a copy of the whole
        database holds every table — but these are the ones the machinery above the seam
        reads and writes on a branch connection.
        """
        return [*get_tables_to_replicate(), self.objectchange_table]

    #
    # Provisioning lifecycle
    #

    @abstractmethod
    def provision(self, branch, user):
        """
        Create the isolated dataset backing ``branch``, or raise.

        Called by ``Branch.provision()``, which owns the surrounding status
        transitions, signal emission and ``BranchEvent`` creation. A backend's
        responsibility is narrowly "make the isolated dataset exist, or raise" —
        including cleaning up its own partial state before raising, so that a
        failed provision does not leave orphaned resources behind.

        A new ``Branch`` has no ``backend_id``: assigning one is part of this
        method's contract, and must happen before the dataset is created, since
        the identifier is how every later connection addresses the branch. Pick a
        value unique across all branches and no longer than 255 characters, and
        persist it with ``branch.set_backend_id()``. The identifier is immutable
        once assigned, so leaving it in place after a failed provision is fine — a
        retry reuses it rather than minting a new one.

        Args:
            branch: The Branch being provisioned
            user: The User who initiated provisioning (may be None)
        """

    @abstractmethod
    def deprovision(self, branch):
        """
        Destroy the isolated dataset backing ``branch``, or raise.

        Called by ``Branch.deprovision()``, which owns the surrounding signal
        emission. Must be safe to call for a branch which was never successfully
        provisioned — including one whose ``backend_id`` is still unset, because
        provisioning never got as far as assigning it.

        A backend which registered connection parameters for the branch must discard
        them here, and should drop the branch's connection with them;
        ``invalidate_connection()`` does both. This is the backend's responsibility
        rather than the model's because only the backend can name the branch's alias
        without risking a failure: the dataset is already gone by the time the caller
        is done, and ``Branch.delete()`` runs all of this inside a transaction, so an
        alias lookup that raises there takes the deletion down with it.

        Args:
            branch: The Branch being deprovisioned
        """

    #
    # Connection addressing
    #

    def connection_alias(self, suffix):
        """
        Return the connection alias formed by appending ``suffix`` to this backend's
        ``connection_alias_prefix``.

        The suffix is whatever the backend addresses a branch by — its ``backend_id`` for
        most backends, something derived from it for others (``SchemaBranchingBackend``
        uses the schema name). Deriving the alias here rather than interpolating the
        prefix at each call site is what keeps the aliases a backend builds while
        provisioning or tearing down identical to the ones it hands the router.

        Raises ValueError for an empty suffix, which is invariant 4: nothing may address a
        branch's dataset before provisioning has named it. The alternative is an alias
        like ``schema_None`` shared by every unprovisioned branch, whose failure surfaces
        far from its cause.
        """
        if not suffix:
            raise ValueError(
                "Cannot derive a connection alias without a branch identifier; the branch "
                "has not yet been provisioned."
            )
        return f'{self.connection_alias_prefix}{suffix}'

    def connection_alias_suffix(self, alias):
        """
        Return the portion of ``alias`` following this backend's ``connection_alias_prefix``,
        or None if ``alias`` does not belong to this backend or carries no suffix at all.

        The inverse of ``connection_alias()``, and the usual opening move in
        ``get_connection_config()``, which must decline an alias that is not its own.
        """
        if not self.owns_connection_alias(alias):
            return None
        return alias.removeprefix(self.connection_alias_prefix) or None

    @abstractmethod
    def get_connection_alias(self, branch):
        """
        Return the Django database connection alias addressing ``branch``.

        This is the single funnel through which every branch-aware query passes
        (via ``Branch.connection_name`` and ``BranchAwareRouter``), and a
        ``Branch`` instance is always in hand. It is therefore the one place a
        backend may read ``branch.connection_params`` and hand them to
        ``register_connection_params()`` for later retrieval by
        ``get_connection_config()``.

        The returned alias must begin with this backend's
        ``connection_alias_prefix``.

        Args:
            branch: The Branch to address

        Returns:
            str: The connection alias
        """

    @abstractmethod
    def get_connection_config(self, alias, default_config):
        """
        Return the ``DATABASES`` entry for ``alias``, or None if ``alias`` does
        not belong to this backend.

        Called by ``DynamicSchemaDict.__getitem__`` from inside Django's
        ``ConnectionHandler`` *while a connection is being created*. It must
        therefore be a pure function of ``alias``, ``settings``, and anything
        previously registered via ``register_connection_params()`` — **it must
        not query the database**, as doing so recurses through connection
        creation.

        Args:
            alias: The connection alias being looked up
            default_config: The ``DATABASES['default']`` entry, to derive from

        Returns:
            dict | None: The connection configuration, or None to defer
        """

    def owns_connection_alias(self, alias):
        """
        Return True if ``alias`` is a branch connection alias belonging to this
        backend. Defaults to a check against ``connection_alias_prefix``.
        """
        return type(alias) is str and alias.startswith(self.connection_alias_prefix)

    def routes_model(self, model, branch):
        """
        Return True if queries for ``model`` should be routed to ``branch``'s
        connection rather than to main.

        Defaults to ``supports_branching(model)``. Overriding this to route more
        models to the branch does not exempt a backend from invariant 3 above.

        Args:
            model: The model class being queried
            branch: The active Branch

        Returns:
            bool
        """
        return supports_branching(model)

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Return True/False to force or forbid migrating ``app_label``/``model_name`` on
        ``db`` — a connection alias this backend owns — or None to express no opinion.

        Called by ``BranchAwareRouter.allow_migrate()``, which has already established
        that ``db`` belongs to this backend; a backend therefore never needs to check
        alias ownership here.

        The default is written for a branch which holds only the branchable tables:
        the plugin's own models and every non-branchable model are refused, because
        their tables do not exist on the branch at all. A backend whose branch is a
        full copy of main's database has the opposite problem — those tables *do*
        exist, and refusing their migrations lets them drift out of step with main
        until a query which selects a column main has and the branch lacks fails — so
        such a backend should override this to permit them.

        Args:
            db: The connection alias being migrated (owned by this backend)
            app_label: The app whose migration is being considered
            model_name: The model being operated on, or None for an operation which
                names no model (``RunPython``, ``RunSQL``)
            hints: Django's routing hints

        Returns:
            bool | None
        """
        # Disallow migrations for models from the plugin itself within a branch
        if app_label == PLUGIN_NAME:
            return False

        # Disallow migrations for models which don't support branching
        if model_name:
            # Permit migrations for the ObjectChange model
            if app_label == 'core' and model_name == 'objectchange':
                return True

            from core.models import ObjectType
            if not ObjectType.objects.using(DEFAULT_DB_ALIAS).filter(
                    app_label=app_label,
                    model=model_name,
                    features__contains=['branching'],
            ).exists():
                return False
        return None

    #
    # Migrations
    #

    def get_pending_migrations(self, branch):
        """
        Return the migrations which have been applied in main but not yet in
        ``branch``, as a list of ``(app_label, name)`` tuples.

        The default asks Django's own migration executor, on the branch's connection, what
        it would still apply. That is correct for any backend whose branch is reachable as
        a Django database — which is every backend, since ``Branch.connection_name`` is how
        everything above this seam reaches one. Override only if a branch's migration state
        lives somewhere other than its own ``django_migrations`` table.

        Args:
            branch: The Branch to inspect

        Returns:
            list[tuple[str, str]]
        """
        connection = connections[branch.connection_name]
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        return [
            (migration.app_label, migration.name) for migration, backward in plan
        ]

    def apply_migrations(self, branch, progress_callback=None):
        """
        Apply any outstanding Django migrations to ``branch``'s dataset.

        Called by ``Branch.migrate()``, which owns the surrounding status
        transitions, signal emission and ``BranchEvent`` creation, and which
        marks the branch FAILED if this raises.

        What this method does is fixed, because it is contract rather than mechanism:
        resolve the branch's connection, build an executor, short-circuit when there is
        nothing to apply, and run the plan **inside ``activate_branch()``** — without which
        ORM queries in a data migration fall through ``BranchAwareRouter`` to the default
        connection and read main, which may already have been migrated past the columns the
        branch's pending migration still depends on.

        A backend which needs to apply the plan differently overrides
        ``run_migration_plan()`` rather than this method.

        Args:
            branch: The Branch to migrate
            progress_callback: An optional callable accepting Django's
                ``MigrationExecutor`` progress-callback signature
                ``(action, migration=None, fake=False)``
        """
        connection = connections[branch.connection_name]
        executor = MigrationExecutor(connection, progress_callback=progress_callback)
        targets = executor.loader.graph.leaf_nodes()
        if not (plan := executor.migration_plan(targets)):
            self.migrate_logger.info("Found no migrations to apply")
            return

        with activate_branch(branch):
            self.run_migration_plan(branch, executor, targets, plan)

    def run_migration_plan(self, branch, executor, targets, plan):
        """
        Apply ``plan`` to ``branch``'s dataset. Called by ``apply_migrations()`` with the
        branch already activated.

        The default is an ordinary ``MigrationExecutor.migrate()``, which is right for a
        backend whose branch is a self-contained database: there is nothing a migration can
        reach that is not the branch's own.

        Override this where that is not true. ``SchemaBranchingBackend`` does, because its
        branch shares a database with main and a ``RunSQL`` body can escape to main's schema
        through the connection's ``search_path``.

        Args:
            branch: The Branch being migrated
            executor: The ``MigrationExecutor`` built for the branch's connection
            targets: The migration targets, i.e. the graph's leaf nodes
            plan: The non-empty plan ``executor`` produced for ``targets``
        """
        executor.migrate(targets)

    #
    # Configuration
    #

    @staticmethod
    def get_backend_config():
        """
        Return the raw ``backend_config`` mapping from the plugin's configuration, or an
        empty dict if there is none.

        Reached from inside Django's connection-creation path, which NetBox's settings
        module enters while still executing, so it must tolerate a ``PLUGINS_CONFIG``
        which does not yet hold this plugin.
        """
        plugin_config = settings.PLUGINS_CONFIG.get(PLUGIN_NAME, {})
        return plugin_config.get(BACKEND_CONFIG_PARAM) or {}

    def get_config(self, name):
        """
        Return the value of this backend's ``name`` configuration parameter.

        Resolution order:

        1. ``PLUGINS_CONFIG['netbox_branching']['backend_config'][name]``, where all
           backend-specific configuration belongs.
        2. ``PLUGINS_CONFIG['netbox_branching'][name]``, for a parameter named in
           ``legacy_config_params`` — deprecated, and warned about at startup.
        3. The default declared in ``default_config``.

        Reads the configuration on every call rather than memoizing it, because a backend
        instance is cached for the life of the process (and across ``override_settings()``
        blocks in tests, which only clear the instance cache on the settings they change).

        Args:
            name: The name of the parameter, which must appear in ``default_config``

        Raises:
            KeyError: If ``name`` is not a parameter of this backend
        """
        if name not in self.default_config:
            raise KeyError(
                f"'{name}' is not a configuration parameter of {type(self).__name__}; a backend's "
                f"parameters are those declared in its default_config mapping."
            )

        plugin_config = settings.PLUGINS_CONFIG.get(PLUGIN_NAME, {})
        backend_config = self.get_backend_config()
        if name in backend_config:
            return backend_config[name]

        # For a legacy parameter, check whether it was defined in the plugin's root config
        if name in self.legacy_config_params and name in plugin_config:
            return get_plugin_config(PLUGIN_NAME, name)

        return self.default_config[name]

    def check_deprecated_config(self):
        """
        Emit a ``FutureWarning`` for each of this backend's parameters found at the root of
        the plugin's configuration rather than under ``backend_config``. Called once from
        the plugin's ``AppConfig.ready()``.
        """
        plugin_config = settings.PLUGINS_CONFIG.get(PLUGIN_NAME, {})
        backend_config = self.get_backend_config()

        for name in self.legacy_config_params:
            if name not in plugin_config:
                continue
            if name in backend_config:
                warnings.warn(
                    f"netbox_branching: '{name}' is set both under 'backend_config' and at the root of "
                    f"PLUGINS_CONFIG['netbox_branching']. The value under 'backend_config' takes effect; "
                    f"remove the root-level one.",
                    FutureWarning,
                )
            else:
                warnings.warn(
                    f"netbox_branching: '{name}' is a parameter of the configured branching backend and "
                    f"belongs under 'backend_config'. Setting it at the root of "
                    f"PLUGINS_CONFIG['netbox_branching'] is deprecated and will stop working in a future "
                    f"release.",
                    FutureWarning,
                )

    def validate_configuration(self):
        """
        Validate the host NetBox configuration this backend requires, raising
        ``ImproperlyConfigured`` if it is unmet. Called once from the plugin's
        ``AppConfig.ready()``, so that a misconfigured host fails at boot rather than on
        the first branch-aware query.

        The two checks here belong to the plugin's own routing machinery rather than to
        any one isolation mechanism, so they hold for every backend: without
        ``DynamicSchemaDict`` wrapping ``DATABASES``, ``get_connection_config()`` is never
        consulted at all and every branch connection fails to exist; without
        ``BranchAwareRouter``, ``get_connection_alias()`` is never called and every
        branch-aware query silently addresses main.

        It also rejects any parameter set under ``backend_config`` which the configured
        backend does not declare in ``default_config``. ``get_config()`` guards the opposite
        direction — a name the backend's own source asks for and has not declared — but
        nothing else catches an operator's typo, which would otherwise resolve to the
        default silently and for good.

        A backend adding checks of its own must call ``super().validate_configuration()``.
        """
        if unknown := set(self.get_backend_config()) - set(self.default_config):
            declared = ', '.join(sorted(self.default_config)) or '(none)'
            raise ImproperlyConfigured(
                f"netbox_branching: unrecognized parameter(s) in "
                f"PLUGINS_CONFIG['{PLUGIN_NAME}']['{BACKEND_CONFIG_PARAM}']: "
                f"{', '.join(sorted(unknown))}. The configured backend "
                f"({type(self).__name__}) accepts: {declared}."
            )

        if type(settings.DATABASES) is not DynamicSchemaDict:
            raise ImproperlyConfigured(
                "netbox_branching: DATABASES must be a DynamicSchemaDict instance."
            )
        if 'netbox_branching.database.BranchAwareRouter' not in settings.DATABASE_ROUTERS:
            raise ImproperlyConfigured(
                "netbox_branching: DATABASE_ROUTERS must contain 'netbox_branching.database.BranchAwareRouter'."
            )

    #
    # Presentation
    #

    def get_detail_fields(self, branch):
        """
        Return extra rows to render on the branch detail page, as a sequence of
        ``(label, value)`` pairs. A value of None renders as a placeholder.

        This is where a backend surfaces facts only it knows — where the branch's dataset
        actually lives, for instance. It defaults to nothing because nothing above this
        seam can know what a given backend's dataset looks like: the page must show what
        the configured backend volunteers, not what the default backend would have done.

        Args:
            branch: The Branch being displayed

        Returns:
            Sequence of (label, value) pairs
        """
        return ()

    #
    # Per-branch connection parameter registry
    #

    @staticmethod
    def _get_connection_params_registry():
        if not hasattr(_connection_params_registry, 'params'):
            _connection_params_registry.params = {}
        return _connection_params_registry.params

    def register_connection_params(self, alias, params):
        """
        Record the connection parameters for ``alias`` so that a subsequent
        ``get_connection_config()`` call — which cannot query the database — can
        retrieve them. Intended to be called from ``get_connection_alias()``.
        """
        self._get_connection_params_registry()[alias] = params

    def unregister_connection_params(self, alias):
        """
        Discard any connection parameters registered for ``alias``.
        """
        self._get_connection_params_registry().pop(alias, None)

    def get_registered_connection_params(self, alias):
        """
        Return the connection parameters previously registered for ``alias``, or
        None if none have been registered in this thread.
        """
        return self._get_connection_params_registry().get(alias)

    def invalidate_connection(self, alias):
        """
        Drop the connection addressing ``alias``, along with any parameters registered
        for it.

        Django calls ``get_connection_config()`` only when it *creates* a connection,
        and keeps the resulting ``settings_dict`` on the ``DatabaseWrapper`` for that
        connection's life. ``close()`` therefore reconnects to the same host and
        credentials however the configuration has changed in the meantime: invalidating
        a connection whose parameters have moved means removing the wrapper, not closing
        it. Doing that by hand is easy to get subtly wrong, which is why it lives here
        rather than in each backend.

        Call it whenever the endpoint behind an alias changes or goes away — a branch
        destroyed and rebuilt under the same name, or one being deprovisioned.

        Both Django's connection handler and the parameter registry are thread-local, so
        this clears the calling thread's state only. A backend whose parameters can
        change must therefore not treat a registered entry as authoritative in a thread
        which has not itself re-derived it. Other threads which have touched the alias
        drop it on their next connection sweep, in close_old_branch_connections().

        Args:
            alias: The connection alias to invalidate
        """
        # hasattr on the handler's private store is how Django itself tests whether an
        # alias has been initialized (see BaseConnectionHandler.all). Going through
        # connections[alias] instead would *create* the very wrapper this removes — and
        # raise, for an alias the backend can no longer configure.
        if hasattr(connections._connections, alias):
            try:
                connections[alias].close()
            except Exception:
                # Teardown: the wrapper is being discarded either way, and a failure to
                # close a connection to a dataset which is already gone is not news.
                logger.debug(f'Unable to close connection {alias}', exc_info=True)
            del connections[alias]

        self.unregister_connection_params(alias)

        # Untrack it too, or the next request_started in this thread rebuilds the very
        # wrapper just removed -- close_old_branch_connections() reaches every tracked
        # alias through connections[alias], which creates one on demand.
        untrack_branch_connection(alias)
