import importlib
import logging
import random
import string
from contextlib import contextmanager

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.operations.special import RunSQL, SeparateDatabaseAndState
from django.db.utils import DatabaseError, ProgrammingError
from netbox.plugins import get_plugin_config
from psycopg.pq import TransactionStatus
from rq.timeouts import JobTimeoutException

from netbox_branching.constants import SKIP_INDEXES
from netbox_branching.provisioning import (
    build_main_constraint_map,
    build_main_index_map,
    build_main_table_sizes,
    parallel_add_constraints,
    parallel_analyze_tables,
    parallel_build_indexes,
    parallel_copy_tables,
    quote_ident,
)
from netbox_branching.utilities import (
    DynamicSchemaDict,
    activate_branch,
    get_tables_to_replicate,
    supports_branching,
)

from .base import BranchingBackend

__all__ = (
    'SchemaBranchingBackend',
)


# pg_catalog.set_config(name, value, is_local=true) is the function-call form
# of SET LOCAL — value is passed as a query parameter rather than interpolated.
_SET_SEARCH_PATH = "SELECT pg_catalog.set_config('search_path', %s, true)"


@contextmanager
def _branch_isolated_runsql(branch_schema, main_schema):
    """
    Restrict ``search_path`` to ``branch_schema`` for each ``RunSQL`` body, then
    restore ``<branch>,<main>`` afterwards. Other operation types keep the
    default search_path because they may need cross-schema visibility (e.g. FKs
    to ``auth.User`` / ``contenttypes``, which aren't replicated to branches).

    Implemented by monkey-patching ``RunSQL.database_forwards`` for the
    duration of the block. Safe because NetBox runs branch migrations as RQ
    jobs (one per worker process); concurrent ``Branch.migrate()`` calls in
    the same process would race.

    A body needing objects from another schema — extension types and operators, which
    live wherever the extension was installed — must put that schema on the path itself;
    NetBox's ltree backfills do so (see ``utilities/mptt_to_ltree.py``). (#617)
    """
    logger = logging.getLogger('netbox_branching.branch.migrate')
    original = RunSQL.database_forwards
    isolated_path = branch_schema
    full_path = f'{branch_schema},{main_schema}'

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        connection = schema_editor.connection

        def set_search_path(value):
            with connection.cursor() as cursor:
                cursor.execute(_SET_SEARCH_PATH, [value])

        set_search_path(isolated_path)
        try:
            return original(self, app_label, schema_editor, from_state, to_state)
        finally:
            try:
                set_search_path(full_path)
            except DatabaseError:
                # Transaction already aborted; don't mask the original failure
                logger.debug(f'Unable to restore search_path after failed RunSQL in {app_label}')

    RunSQL.database_forwards = database_forwards
    try:
        yield
    finally:
        RunSQL.database_forwards = original


def _fake_for_branch(migration):
    """
    Return True if a migration should be faked when applied to a branch schema, False otherwise.

    Decision order:
    1. If the migration module sets a ``fake_on_branch`` attribute, that value is respected
       directly: ``True`` forces faking, ``False`` forces the migration to run.
    2. Otherwise, fall back to a heuristic: fake migrations whose model-specific operations
       affect only non-branchable models. This prevents RunSQL operations from inadvertently
       acting on the main (public) schema via the search_path.

    Migrations with no model-specific operations (e.g. pure RunSQL or RunPython) are not faked
    by the heuristic, as we cannot determine their intent without executing them. Authors of
    such migrations should set ``fake_on_branch`` explicitly when needed.

    SeparateDatabaseAndState operations are not supported and will be skipped with an error.

    Faking is schema-backend policy rather than general policy: it exists specifically to stop
    ``RunSQL`` bodies from reaching the main schema through the branch connection's search_path.
    """
    logger = logging.getLogger('netbox_branching.branch.migrate')

    # Check for an explicit per-migration override
    try:
        module = importlib.import_module(f'{migration.app_label}.migrations.{migration.name}')
    except ModuleNotFoundError:
        module = None
    if module is not None and (explicit := getattr(module, 'fake_on_branch', None)) is not None:
        return bool(explicit)

    has_model_operations = False
    for operation in migration.operations:
        if isinstance(operation, SeparateDatabaseAndState):
            logger.error(
                f"Migration {migration} contains SeparateDatabaseAndState, which is not supported "
                f"for branch schema migration. This migration will not be faked."
            )
            return False
        if (model_name := getattr(operation, 'model_name', None)) is None:
            continue
        has_model_operations = True
        # If any operation targets a branchable model, don't fake this migration
        try:
            model = apps.get_model(migration.app_label, model_name)
        except LookupError:
            # If we can't resolve the model (e.g. removed in a squashed migration),
            # conservatively treat it as branchable and don't fake.
            logger.warning(f"Could not resolve model {migration.app_label}.{model_name}; not faking {migration}")
            return False
        if supports_branching(model):
            return False
    return has_model_operations


class SchemaBranchingBackend(BranchingBackend):
    """
    The default branching backend: isolates each branch in its own PostgreSQL schema
    within the same database as main, populated by replicating main's branchable
    tables at provision time.

    Branch connections are ordinary connections to the default database with
    ``search_path`` set to ``<branch schema>,<main schema>``, so unreplicated
    tables (``auth.User``, ``contenttypes``, the plugin's own models) resolve
    against main and joins across the boundary work.

    A branch's schema name is its ``backend_id`` prefixed with ``schema_prefix``,
    so the identifier this backend assigns during provisioning is deliberately
    short: PostgreSQL truncates identifiers beyond 63 bytes, and two branches whose
    schema names collided after truncation would share a single schema.

    Configured by the ``main_schema``, ``schema_prefix`` and ``provision_workers``
    plugin parameters.
    """
    connection_alias_prefix = 'schema_'

    # PostgreSQL's NAMEDATALEN-1 limit on identifiers, in bytes
    MAX_IDENTIFIER_LENGTH = 63

    #
    # Branch identity
    #

    def generate_branch_id(self, length=8):
        """
        Generate a random alphanumeric identifier for a new branch, short enough that
        ``schema_prefix`` plus the identifier stays within PostgreSQL's limit on
        identifier length. Retries on the (vanishingly improbable) event of a
        collision with an existing branch.
        """
        from netbox_branching.models import Branch

        chars = [*string.ascii_lowercase, *string.digits]
        for _ in range(10):
            branch_id = ''.join(random.choices(chars, k=length))
            if not Branch.objects.filter(backend_id=branch_id).exists():
                return branch_id
        raise RuntimeError("Failed to generate a unique branch ID after 10 attempts")

    #
    # Connection addressing
    #

    def get_connection_alias(self, branch):
        return f'{self.connection_alias_prefix}{branch.schema_name}'

    def get_connection_config(self, alias, default_config):
        if not self.owns_connection_alias(alias):
            return None
        if not (schema := alias.removeprefix(self.connection_alias_prefix)):
            return None

        main_schema = get_plugin_config('netbox_branching', 'main_schema', 'public')
        return {
            **default_config,
            'OPTIONS': {
                **default_config.get('OPTIONS', {}),
                'options': f'-c search_path={schema},{main_schema}'
            },
        }

    #
    # Migrations
    #

    def get_pending_migrations(self, branch):
        connection = connections[branch.connection_name]
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
        return [
            (migration.app_label, migration.name) for migration, backward in plan
        ]

    def apply_migrations(self, branch, progress_callback=None):
        logger = logging.getLogger('netbox_branching.branch.migrate')

        connection = connections[branch.connection_name]
        executor = MigrationExecutor(connection, progress_callback=progress_callback)
        targets = executor.loader.graph.leaf_nodes()
        main_schema = get_plugin_config('netbox_branching', 'main_schema')
        if not (plan := executor.migration_plan(targets)):
            logger.info("Found no migrations to apply")
            return

        # Activate the branch so that any ORM queries inside data migrations
        # (RunPython) route to the branch schema rather than main. Without this,
        # historical-model queries fall through the BranchAwareRouter to the default
        # connection and read from main, which may have already been migrated past
        # columns the branch's pending migration still depends on.
        with activate_branch(branch), _branch_isolated_runsql(branch.schema_name, main_schema):
            # Apply each migration individually, faking those that only affect
            # non-branchable models to prevent RunSQL from inadvertently operating
            # on the main schema via the search_path. See GitHub issue #423.
            full_plan = executor.migration_plan(executor.loader.graph.leaf_nodes(), clean_start=True)
            migrations_to_run = {m for m, _ in plan}
            # _create_project_state is a private Django API (MigrationExecutor). It builds
            # the current ProjectState from all applied migrations, which apply_migration
            # requires as its starting point. There is no public equivalent as of Django 5.x.
            state = executor._create_project_state(with_applied_migrations=True)
            for migration, _ in full_plan:
                if not migrations_to_run:
                    break
                if migration in migrations_to_run:
                    fake = _fake_for_branch(migration)
                    state = executor.apply_migration(state, migration, fake=fake)
                    if fake:
                        # apply_migration() doesn't advance the state when faking, leaving
                        # later data migrations with stale historical models. (#617)
                        state = migration.mutate_state(state, preserve=False)
                    migrations_to_run.remove(migration)

    #
    # Configuration
    #

    def validate_configuration(self):
        if type(settings.DATABASES) is not DynamicSchemaDict:
            raise ImproperlyConfigured(
                "netbox_branching: DATABASES must be a DynamicSchemaDict instance."
            )
        if 'netbox_branching.database.BranchAwareRouter' not in settings.DATABASE_ROUTERS:
            raise ImproperlyConfigured(
                "netbox_branching: DATABASE_ROUTERS must contain 'netbox_branching.database.BranchAwareRouter'."
            )

        # Validate provision_workers up front rather than letting a bad value surface as an
        # unhandled error only when a branch is first provisioned.
        workers = get_plugin_config('netbox_branching', 'provision_workers')
        if workers is not None:
            if type(workers) is not int:
                raise ImproperlyConfigured(
                    "netbox_branching: 'provision_workers' must be an integer."
                )
            if workers < 1:
                raise ImproperlyConfigured(
                    "netbox_branching: 'provision_workers' must be greater than or equal to 1."
                )

    #
    # Provisioning lifecycle
    #

    def provision(self, branch, user):
        """
        Create the schema & replicate main tables.

        Five phases:
          1. Metadata setup — create the schema, the ObjectChange skeleton, the
             django_migrations copy, and the empty (no constraints, no indexes)
             destination tables. The constraint and index maps for main are
             also captured here for use in Phase 3.
          2. Parallel data copy — export an MVCC snapshot from main and let a worker
             pool run INSERT INTO branch.t SELECT * FROM main.t across tables.
          3. Parallel constraint + index build — add PK/UNIQUE/EXCLUDE constraints
             (each one builds its backing index implicitly under the original
             name) and then replay every remaining indexdef against the populated
             branch tables.
          4. Replicate triggers — CREATE TABLE ... (LIKE ...) does not carry
             triggers, so copy every non-internal trigger from main's copied
             tables onto the branch tables. NetBox 4.7+ maintains ltree
             path/sort_path columns and denormalized _site/_location/_rack columns
             via per-table triggers (formerly Python signal handlers); without
             them, writes inside a branch would leave those columns stale. Done
             after Phase 2 so the triggers don't fire on the bulk snapshot load.
          5. ANALYZE the populated tables so the planner has real statistics
             immediately rather than waiting for autovacuum.

        On any failure the (possibly partial) schema is dropped before the
        exception propagates.
        """
        # Imported here rather than at module scope so that resolving this backend
        # (which can happen while a database connection is being created, potentially
        # before the app registry is fully populated) never needs the model registry.
        from core.models import ObjectChange as ObjectChange_

        logger = logging.getLogger('netbox_branching.branch.provision')
        main_schema = get_plugin_config('netbox_branching', 'main_schema')
        workers = get_plugin_config('netbox_branching', 'provision_workers') or 1

        # Assign this branch its identifier, which determines its schema name. An
        # identifier already in place is reused, so re-provisioning an archived branch
        # recreates the schema it had before.
        if not branch.backend_id:
            branch.set_backend_id(self.generate_branch_id())

        schema = branch.schema_name
        if len(schema.encode()) > self.MAX_IDENTIFIER_LENGTH:
            raise ImproperlyConfigured(
                f"Schema name '{schema}' exceeds PostgreSQL's {self.MAX_IDENTIFIER_LENGTH}-byte limit on "
                "identifiers. Shorten the schema_prefix configuration parameter or the branch identifier "
                "generated by this backend."
            )
        tables_to_replicate = get_tables_to_replicate()

        try:
            # Phase 1: metadata setup. Done in a single committed transaction so
            # the workers in Phase 2 (which run on separate connections) can see
            # the newly-created schema and tables.
            with connection.cursor() as cursor:
                cursor.execute("BEGIN")
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

                # A fresh branch's schema (named for its unique, randomly-generated
                # backend_id) should not already exist. If it does, it's an orphan left by a
                # previous provision of THIS branch that was hard-killed (OOM, SIGKILL, lost
                # connection) between Phase 1's commit and the cleanup path below; drop it so
                # the CREATE doesn't fail with "schema already exists". Only drop when it
                # actually exists, and log loudly when we do — this DROP ... CASCADE is the
                # one place provisioning destroys data, so its (rare, expected-only-after-an-
                # interrupted-provision) firing must be visible rather than silent and
                # unconditional.
                logger.debug(f'Creating schema {schema}')
                cursor.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [schema]
                )
                if cursor.fetchone():
                    logger.warning(
                        f"Schema {schema} already exists at provision time; dropping it before "
                        f"recreating. This is expected only after a previously interrupted "
                        f"provision of this branch."
                    )
                    cursor.execute(f"DROP SCHEMA IF EXISTS {quote_ident(schema)} CASCADE")
                try:
                    cursor.execute(f"CREATE SCHEMA {quote_ident(schema)}")
                except ProgrammingError as e:
                    if str(e).startswith('permission denied '):
                        logger.critical(
                            f"Provisioning failed due to insufficient database permissions. Ensure that the NetBox "
                            f"role ({settings.DATABASE['USER']}) has permission to create new schemas on this "
                            f"database ({settings.DATABASE['NAME']}). (Use the PostgreSQL command 'GRANT CREATE ON "
                            f"DATABASE $database TO $role;' to grant the required permission.)"
                        )
                    raise

                # Prefetch every index definition on main in one query — the per-table
                # entries drive the post-data-load index build.
                main_indexes_by_table = build_main_index_map(cursor, main_schema)
                # Same for PRIMARY KEY / UNIQUE / EXCLUDE constraints. We replay
                # these via ALTER TABLE ADD CONSTRAINT so the branch schema's
                # pg_constraint mirrors main's — necessary for later migrations
                # that drop or alter constraints by name.
                main_constraints_by_table = build_main_constraint_map(cursor, main_schema)
                # On-disk size per table, used to dispatch the heaviest tables first in
                # each parallel phase so a single large table can't be picked up last and
                # left running alone while every other worker sits idle.
                main_table_sizes = build_main_table_sizes(cursor, main_schema)

                # Empty copy of the global change log. Share the ID sequence from main
                # so change record IDs stay globally unique.
                objectchange_table = ObjectChange_._meta.db_table
                main_objectchange = f'{quote_ident(main_schema)}.{quote_ident(objectchange_table)}'
                branch_objectchange = f'{quote_ident(schema)}.{quote_ident(objectchange_table)}'
                logger.debug(f'Creating table {schema}.{objectchange_table}')
                cursor.execute(f"CREATE TABLE {branch_objectchange} ( LIKE {main_objectchange} )")
                # Look the sequence up dynamically rather than assuming the
                # <table>_id_seq naming convention (matches the Phase 2 copy).
                cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [main_objectchange])
                row = cursor.fetchone()
                if row and row[0]:
                    cursor.execute(
                        f"ALTER TABLE {branch_objectchange} ALTER COLUMN id SET DEFAULT nextval(%s)",
                        [row[0]],
                    )

                # Copy the django_migrations table
                branch_migrations = f'{quote_ident(schema)}.django_migrations'
                main_migrations = f'{quote_ident(main_schema)}.django_migrations'
                logger.debug(f'Creating table {schema}.django_migrations')
                cursor.execute(f"CREATE TABLE {branch_migrations} ( LIKE {main_migrations} )")
                cursor.execute(f"INSERT INTO {branch_migrations} SELECT * FROM {main_migrations}")
                cursor.execute(
                    f"ALTER TABLE {branch_migrations} ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY"
                )
                # COALESCE guards against an empty django_migrations on main: MAX
                # of no rows returns NULL, which would TypeError on + 1 below.
                cursor.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {branch_migrations}")
                starting_id = cursor.fetchone()[0]
                cursor.execute(
                    f"ALTER SEQUENCE {quote_ident(schema)}.django_migrations_id_seq RESTART WITH {starting_id}"
                )

                # Create empty destination tables (no indexes) for the parallel copy.
                # Indexes are built in Phase 3 after the data is loaded — far cheaper
                # than maintaining them row-by-row during INSERT.
                for table in tables_to_replicate:
                    logger.debug(f'Creating table {schema}.{table}')
                    cursor.execute(
                        f"CREATE TABLE {quote_ident(schema)}.{quote_ident(table)} "
                        f"( LIKE {quote_ident(main_schema)}.{quote_ident(table)} )"
                    )

                cursor.execute("COMMIT")

            # Order parallel work heaviest-table-first (longest-processing-time
            # scheduling) so a single large table can't be dispatched last and left
            # running alone while the other workers idle. Used by every phase below.
            def by_size_desc(table_names):
                return sorted(table_names, key=lambda t: main_table_sizes.get(t, 0), reverse=True)

            # Phase 2: parallel data copy under a single MVCC snapshot.
            # The coordinator transaction holds the exported snapshot alive while
            # workers import it; do not commit until every worker has finished.
            coordinator_commit_failed = False
            with connection.cursor() as cursor:
                cursor.execute("BEGIN")
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute("SELECT pg_export_snapshot()")
                snapshot_token = cursor.fetchone()[0]
                logger.debug(f'Exported snapshot {snapshot_token} for {len(tables_to_replicate)} tables')

                try:
                    parallel_copy_tables(
                        tables=by_size_desc(tables_to_replicate),
                        snapshot_token=snapshot_token,
                        schema=schema,
                        main_schema=main_schema,
                        workers=workers,
                    )
                finally:
                    # Close the snapshot-exporting transaction. Swallow any error
                    # here so it can't mask a worker exception that's already in
                    # flight — the original traceback is what the operator needs.
                    try:
                        cursor.execute("COMMIT")
                    except Exception:
                        coordinator_commit_failed = True
                        logger.exception(
                            "Failed to COMMIT Phase 2 coordinator transaction"
                        )

            # The copied data is already durable (each worker committed its own
            # transaction) and this coordinator transaction was read-only, so a failed
            # COMMIT is not fatal. But it can leave the main connection in an aborted
            # transaction, which would then break the success-path status=READY write
            # (and the cleanup path) with "current transaction is aborted". Drop the
            # connection so Django reconnects clean for the remaining ORM work.
            if coordinator_commit_failed:
                connection.close()

            # Phase 3: rebuild constraints and indexes against the populated
            # branch tables. PK/UNIQUE/EXCLUDE constraints are added via
            # ALTER TABLE ADD CONSTRAINT — each one builds its backing index
            # implicitly under the constraint's name, so we exclude those
            # index names from the plain CREATE INDEX pass to avoid duplicates.
            relevant_tables = {*tables_to_replicate, objectchange_table, 'django_migrations'}

            sorted_relevant = by_size_desc(relevant_tables)

            constraint_tasks = []
            constraint_backed_indexes = set()
            index_tasks = []
            # Build both task lists heaviest-table-first so the constraint and index
            # phases drain their largest work early rather than tailing on it.
            for table_name in sorted_relevant:
                for conname, condef, backing_indexname in main_constraints_by_table.get(table_name, ()):
                    constraint_tasks.append((table_name, conname, condef))
                    if backing_indexname:
                        constraint_backed_indexes.add(backing_indexname)
                for indexname, indexdef in main_indexes_by_table.get(table_name, ()):
                    index_tasks.append((table_name, indexname, indexdef))

            parallel_add_constraints(
                constraint_tasks=constraint_tasks,
                schema=schema,
                workers=workers,
            )

            parallel_build_indexes(
                index_tasks=index_tasks,
                schema=schema,
                main_schema=main_schema,
                workers=workers,
                skip_indexes={*SKIP_INDEXES, *constraint_backed_indexes},
            )

            # Phase 4: replicate triggers. CREATE TABLE ... (LIKE ...) does not
            # copy triggers, and NetBox 4.7+ relies on per-table triggers to keep
            # ltree path/sort_path and denormalized _site/_location/_rack columns
            # up to date (both were previously maintained in Python). Install them
            # only after the Phase 2 data copy so they don't fire per row on the
            # bulk snapshot load, which already carries correct values from main.
            self._replicate_triggers(schema, main_schema, relevant_tables)

            # Phase 5: refresh planner statistics. After the bulk copy the branch
            # tables have no statistics, so the first queries against the branch
            # (sync, change-diff computation, etc.) would plan against empty-table
            # estimates until autovacuum eventually catches up. ANALYZE is
            # statistics-only and never affects correctness, so a failure here must
            # not fail the provision — log it and leave the stats to autovacuum.
            #
            # Skip empty tables: a zero-size table's planner estimate is already
            # correct (there is nothing to mis-estimate), and a typical install has
            # hundreds of empty branchable tables — ANALYZE-ing every one of them adds
            # a fixed hundreds-of-statements tax that can dominate a small provision.
            analyze_tables = [t for t in sorted_relevant if main_table_sizes.get(t, 0) > 0]
            try:
                parallel_analyze_tables(
                    tables=analyze_tables,
                    schema=schema,
                    workers=workers,
                )
            except JobTimeoutException:
                # RQ is killing the job via its timeout (raised in this, the main,
                # thread). It is an Exception subclass, so it would otherwise be
                # swallowed by the best-effort handler below — let it propagate to
                # the cleanup path instead of marking a half-provisioned branch READY.
                raise
            except Exception:
                logger.warning(
                    f"ANALYZE of branch schema {schema} failed; planner statistics will be "
                    f"populated by autovacuum instead.",
                    exc_info=True,
                )

        except Exception as e:
            logger.error(e)
            # If Phase 1 raised mid-transaction the connection is in an aborted
            # state; clear it before running cleanup or the DROP SCHEMA and the
            # status update in Branch.provision() would both fail with "current
            # transaction is aborted". A Phase 2/3 failure leaves the connection
            # idle (the coordinator already committed and the workers use their own
            # connections), so only issue the ROLLBACK when the server actually
            # has an open transaction — an out-of-transaction ROLLBACK would emit
            # a spurious "no transaction in progress" warning.
            if connection.connection is not None and \
                    connection.connection.info.transaction_status != TransactionStatus.IDLE:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("ROLLBACK")
                except Exception:
                    logger.exception(f"Failed to roll back aborted transaction for {schema}")
            # Clean up any partial state from the failed provision.
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP SCHEMA IF EXISTS {quote_ident(schema)} CASCADE")
            except Exception:
                logger.exception(f"Failed to drop schema {schema} during provision cleanup")
            raise

    def _replicate_triggers(self, schema, main_schema, tables):
        """
        Copy user-defined triggers from the main schema's tables onto the branch
        schema's copies of those tables.

        ``CREATE TABLE ... (LIKE ...)`` does not carry triggers, and since NetBox
        4.7 several behaviours that used to live in Python signal handlers are
        implemented as per-table PostgreSQL triggers — ltree path/sort_path
        maintenance and the denormalized _site/_location/_rack columns. Without
        replicating them, a write inside a branch (reparenting a Region, moving a
        Device between sites, etc.) would leave those columns stale.

        The trigger functions are defined in the main schema and address their
        target table by *unqualified* name, resolving it through ``search_path``
        at execution time. Recreating a trigger under ``search_path =
        <branch>,<main>`` therefore binds it to the branch table (searched first)
        while still reusing the main schema's function (searched second) — so no
        per-branch functions are created and the trigger operates only on branch
        data. Internal (constraint/FK-enforcement) triggers are excluded; those
        are rebuilt by the Phase 3 constraint pass.

        Two consequences of resolving the target through ``search_path``:

        * Any raw SQL which writes a branch table by *schema-qualified* name from a
          connection whose ``search_path`` is the main schema (as the provisioning
          copy statements do) would have its triggers read and write main's tables.
          Such statements are only safe against tables which carry no triggers, or
          when run on a connection with the branch schema searched first.
        * NetBox's ltree triggers take a per-tree advisory lock keyed on
          ``TG_TABLE_NAME``, which carries no schema. A branch and the main schema
          therefore contend on the same key for the same tree, and because the
          cross-tree path takes two locks, a branch operation and a main operation
          touching the same pair of trees in opposite order can deadlock.
        """
        logger = logging.getLogger('netbox_branching.branch.provision')
        with connection.cursor() as cursor:
            cursor.execute("BEGIN")
            try:
                # Fetch trigger definitions with the main schema alone on the
                # search_path so pg_get_triggerdef emits unqualified table and
                # function names (both live in the main schema). Emitting them
                # qualified would rebind the recreated trigger to the main table.
                cursor.execute(f"SET LOCAL search_path = {quote_ident(main_schema)}")
                cursor.execute(
                    """
                    SELECT c.relname, pg_get_triggerdef(t.oid, true)
                    FROM pg_trigger t
                    JOIN pg_class c ON t.tgrelid = c.oid
                    JOIN pg_namespace n ON c.relnamespace = n.oid
                    WHERE n.nspname = %s
                      AND NOT t.tgisinternal
                      AND c.relname = ANY(%s)
                    """,
                    [main_schema, list(tables)],
                )
                triggerdefs = cursor.fetchall()

                if triggerdefs:
                    # Recreate each trigger with the branch schema searched first so
                    # the unqualified table name binds to the branch copy, while the
                    # unqualified function name still resolves in the main schema.
                    cursor.execute(
                        f"SET LOCAL search_path = {quote_ident(schema)}, {quote_ident(main_schema)}"
                    )
                    for table, triggerdef in triggerdefs:
                        logger.debug(f'Replicating trigger onto {schema}.{table}')
                        cursor.execute(triggerdef)

                cursor.execute("COMMIT")
                logger.debug(f'Replicated {len(triggerdefs)} trigger(s) onto schema {schema}')
            except Exception:
                try:
                    cursor.execute("ROLLBACK")
                except Exception:
                    # Don't let a broken connection mask the original failure
                    logger.exception(f'Failed to roll back trigger replication for {schema}')
                raise

    def deprovision(self, branch):
        logger = logging.getLogger('netbox_branching.branch.provision')

        if not branch.backend_id:
            # Provisioning never got as far as assigning an identifier, so there is no
            # schema to drop.
            logger.debug(f'Branch {branch} has no backend ID; nothing to deprovision')
            return

        with connection.cursor() as cursor:
            # Delete the schema and all its tables
            logger.debug(f'Deleting schema {branch.schema_name}')
            cursor.execute(
                f"DROP SCHEMA IF EXISTS {quote_ident(branch.schema_name)} CASCADE"
            )
