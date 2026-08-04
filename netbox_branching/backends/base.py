from abc import ABC, abstractmethod

from asgiref.local import Local
from django.db import DEFAULT_DB_ALIAS

from netbox_branching.utilities import supports_branching

__all__ = (
    'BranchingBackend',
)


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

    # Prefix identifying connection aliases owned by this backend. Retained as
    # 'schema_' by the default backend for backward compatibility with existing
    # installs; a backend must not reuse another backend's prefix.
    connection_alias_prefix = 'schema_'

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

        Args:
            branch: The Branch being deprovisioned
        """

    #
    # Connection addressing
    #

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
        if app_label == 'netbox_branching':
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

    @abstractmethod
    def get_pending_migrations(self, branch):
        """
        Return the migrations which have been applied in main but not yet in
        ``branch``, as a list of ``(app_label, name)`` tuples.

        Args:
            branch: The Branch to inspect

        Returns:
            list[tuple[str, str]]
        """

    @abstractmethod
    def apply_migrations(self, branch, progress_callback=None):
        """
        Apply any outstanding Django migrations to ``branch``'s dataset.

        Called by ``Branch.migrate()``, which owns the surrounding status
        transitions, signal emission and ``BranchEvent`` creation, and which
        marks the branch FAILED if this raises.

        Args:
            branch: The Branch to migrate
            progress_callback: An optional callable accepting Django's
                ``MigrationExecutor`` progress-callback signature
                ``(action, migration=None, fake=False)``
        """

    #
    # Configuration
    #

    def validate_configuration(self):
        """
        Validate any host NetBox configuration this backend requires, raising
        ``ImproperlyConfigured`` if it is unmet. Called once from the plugin's
        ``AppConfig.ready()``. Defaults to a no-op.
        """

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

    def get_registered_connection_params(self, alias):
        """
        Return the connection parameters previously registered for ``alias``, or
        None if none have been registered in this thread.
        """
        return self._get_connection_params_registry().get(alias)
