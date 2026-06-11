"""
Helpers for parallelizing the data-copy, index-build, and constraint-add passes
of Branch.provision().

The single-process approach used previously is fine for small databases but becomes
the dominant cost on installs with multi-GB branchable data. This module splits the
two heaviest phases across a thread pool, using PostgreSQL's exported snapshot
mechanism (the same one pg_dump --jobs uses) so that every worker reads main from
an identical MVCC view.
"""
import logging
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import DEFAULT_DB_ALIAS, connections
from psycopg import sql

__all__ = (
    'build_main_constraint_map',
    'build_main_index_map',
    'build_main_table_sizes',
    'parallel_add_constraints',
    'parallel_analyze_tables',
    'parallel_build_indexes',
    'parallel_copy_tables',
    'quote_ident',
)

logger = logging.getLogger('netbox_branching.branch.provision')

# pg_export_snapshot() returns a token of the form "<digits>-<digits>-<digits>"
# (sometimes with hex chars). We validate before interpolating because SET TRANSACTION
# SNAPSHOT is a utility statement that does not accept bind parameters in all drivers.
_SNAPSHOT_TOKEN_RE = re.compile(r'\A[A-Fa-f0-9\-]+\Z')


def quote_ident(identifier):
    """Quote a single SQL identifier for safe interpolation into a DDL string.

    Always wraps the name in double quotes and escapes any embedded ones, so reserved
    words, mixed-case names, or odd characters can't break (or inject into) the
    statement. We delegate the escaping to psycopg's own ``sql.Identifier`` rather than
    hand-rolling it — the driver is the authority on its dialect, and Django's
    ``connection.ops.quote_name`` is unsuitable here because it does not escape embedded
    quotes.

    Note this always-quote form is intentionally distinct from the server-side
    ``quote_ident()`` used in ``parallel_build_indexes`` to rewrite index definitions:
    that one must emit PostgreSQL's canonical (bare-when-possible) form to match what
    ``pg_get_indexdef`` produces.
    """
    return sql.Identifier(identifier).as_string()


def _cancel_backends(pids):
    """Best-effort `pg_cancel_backend` for a list of worker PIDs. Used after the
    first worker failure so that any still-running workers' queries terminate
    before the cleanup DROP SCHEMA tries to take ACCESS EXCLUSIVE on their tables.

    Uses a brand-new connection rather than the caller's thread-local one — the
    Phase 2 coordinator holds an open snapshot-exporting transaction on the main
    thread's connection, and closing that connection here would invalidate the
    exported snapshot before workers have observed their failures.
    """
    if not pids:
        return
    conn = None
    try:
        # Opening this connection can itself fail under the very condition that tends
        # to trigger a worker failure (e.g. the server at max_connections). Keep it
        # inside the try so the failure is caught rather than propagated through the
        # worker's error handler.
        conn = connections.create_connection(DEFAULT_DB_ALIAS)
        with conn.cursor() as cursor:
            for pid in pids:
                try:
                    cursor.execute("SELECT pg_cancel_backend(%s)", [pid])
                except Exception:
                    logger.exception(f"Failed to cancel worker backend {pid}")
    except Exception:
        # If we couldn't cancel the in-flight workers, the downstream DROP SCHEMA
        # cleanup will block on their table locks until they finish naturally —
        # potentially minutes on a loaded system. Warn loudly so the apparent hang
        # is diagnosable.
        logger.warning(
            "Failed to cancel in-flight provisioning workers; schema cleanup may block "
            "until they finish on their own.",
            exc_info=True,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.debug("Ignoring error while closing cancellation connection", exc_info=True)


def _run_pool(tasks, label, workers):
    """Run each task — a callable taking a single cursor argument — across a pool
    of ``workers`` threads.

    Each worker thread opens ONE dedicated backend (via ``create_connection`` so it
    is independent of the thread-local connection cache) and pulls tasks from a
    shared queue until it is drained, reusing that one connection for every task it
    runs. This keeps the number of backends established per provision proportional
    to ``workers`` rather than to the number of tasks — establishing a fresh
    PostgreSQL backend per table/index/constraint is expensive and, during the copy
    phase, needlessly lengthens the window the coordinator must hold its MVCC
    snapshot open.

    On the first task failure: record it, signal cancel_event so idle workers stop
    pulling new tasks, and pg_cancel_backend the in-flight workers' queries so the
    downstream DROP SCHEMA cleanup isn't blocked on their locks. The original
    exception is re-raised once the pool drains.
    """
    tasks = list(tasks)
    if not tasks:
        return
    # Never spin up more workers than there is work — each worker eagerly opens its
    # own backend, so excess workers would establish and immediately close idle
    # connections. In practice the provisioning call sites always pass far more tasks
    # than workers; this only matters for short task lists.
    workers = max(1, min(int(workers), len(tasks)))
    task_iter = iter(tasks)
    task_lock = threading.Lock()
    pids = []
    pids_lock = threading.Lock()
    cancel_event = threading.Event()
    errors = []
    errors_lock = threading.Lock()

    def next_task():
        with task_lock:
            return next(task_iter, None)

    def record_failure(exc):
        with errors_lock:
            first = not errors
            errors.append(exc)
        if first:
            logger.error(f"{label} worker failed", exc_info=exc)
            cancel_event.set()
            with pids_lock:
                to_cancel = list(pids)
            _cancel_backends(to_cancel)

    def worker():
        conn = None
        try:
            # create_connection() builds a connection that is NOT registered in the
            # thread-local cache, so each worker owns a private backend it can safely
            # close without disturbing the main thread's connection (which, during the
            # copy phase, holds the open snapshot-exporting transaction). connections[alias]
            # would hand back a shared, thread-local wrapper instead. This is a semi-public
            # Django API (it lives outside the documented surface but is long-stable); do
            # not "simplify" it to connections[alias].
            conn = connections.create_connection(DEFAULT_DB_ALIAS)
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                with pids_lock:
                    pids.append(cursor.fetchone()[0])
            while not cancel_event.is_set():
                task = next_task()
                if task is None:
                    break
                with conn.cursor() as cursor:
                    task(cursor)
        except Exception as e:  # noqa: BLE001 — any task failure must abort the whole pool
            record_failure(e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.debug("Ignoring error while closing worker connection", exc_info=True)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=label) as pool:
        futures = [pool.submit(worker) for _ in range(workers)]
        for future in as_completed(futures):
            # worker() funnels task failures into `errors`; this only surfaces an
            # unexpected error escaping worker() itself.
            future.result()

    if errors:
        raise errors[0]


def build_main_index_map(cursor, main_schema):
    """
    Return ``{tablename: [(indexname, indexdef), ...]}`` for every index in the
    main schema. ``indexdef`` is PostgreSQL's reconstructed CREATE INDEX statement;
    we replay it against the branch schema after data is loaded.
    """
    cursor.execute(
        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = %s",
        [main_schema],
    )
    result = defaultdict(list)
    for tablename, indexname, indexdef in cursor.fetchall():
        result[tablename].append((indexname, indexdef))
    return result


def build_main_table_sizes(cursor, main_schema):
    """
    Return ``{tablename: size_in_bytes}`` for every ordinary table in the main schema.

    ``pg_table_size`` (heap + TOAST, excluding indexes) is an always-accurate proxy for
    how much work copying and indexing a table will take — unlike ``pg_class.reltuples``
    it does not depend on a recent ANALYZE. Callers use it only to order parallel work
    heaviest-first, so an approximate ranking is sufficient; tables absent from the map
    simply sort last.
    """
    cursor.execute(
        """
        SELECT cls.relname, pg_table_size(cls.oid)
        FROM pg_class cls
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        WHERE ns.nspname = %s
          AND cls.relkind = 'r'
        """,
        [main_schema],
    )
    return {tablename: size for tablename, size in cursor.fetchall()}


def build_main_constraint_map(cursor, main_schema):
    """
    Return ``{tablename: [(conname, condef, backing_indexname), ...]}`` for every
    PRIMARY KEY, UNIQUE, and EXCLUDE constraint in the main schema. ``condef`` is
    the reconstructed constraint clause (e.g. ``PRIMARY KEY (id)``) used in
    ``ALTER TABLE ... ADD CONSTRAINT name <condef>``. ``backing_indexname`` is the
    index PG creates implicitly for the constraint — callers exclude it from the
    plain CREATE INDEX pass to avoid duplicates.

    The previous `LIKE main.t INCLUDING INDEXES` semantics copied these as
    real constraints; we replay them as ALTER TABLE ADD CONSTRAINT so future
    migrations against the branch (DROP CONSTRAINT, AlterUniqueTogether, etc.)
    find them.

    FOREIGN KEY ('f') and CHECK ('c') constraints are intentionally excluded, matching
    the prior `LIKE INCLUDING INDEXES` behaviour (which replicated neither). Branch
    schemas are not subjected to FK enforcement against main, and CHECK constraints are
    re-derived by any migration that needs them. A migration that drops a CHECK
    constraint by name on a branch would therefore not find it — the same limitation
    that existed before this change.
    """
    cursor.execute(
        """
        SELECT
            cls.relname AS tablename,
            con.conname,
            pg_get_constraintdef(con.oid) AS condef,
            idx.relname AS backing_indexname
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        LEFT JOIN pg_class idx ON idx.oid = con.conindid
        WHERE ns.nspname = %s
          AND con.contype IN ('p', 'u', 'x')
        """,
        [main_schema],
    )
    result = defaultdict(list)
    for tablename, conname, condef, backing_indexname in cursor.fetchall():
        result[tablename].append((conname, condef, backing_indexname))
    return result


def parallel_copy_tables(tables, snapshot_token, schema, main_schema, workers):
    """
    Copy ``tables`` from ``main_schema`` into ``schema`` across a worker pool.

    Each worker opens its own database connection on its thread, starts a
    REPEATABLE READ transaction, imports the supplied snapshot so every worker
    sees an identical MVCC view of main, and runs ``INSERT INTO ... SELECT *``
    for the tables assigned to it. The destination tables must already exist
    and must be empty (i.e. created by the coordinator with ``CREATE TABLE
    branch.t ( LIKE main.t )``, without indexes).

    The exporting transaction on the coordinator must remain open while this
    runs — the snapshot is only valid for import while the exporter is alive.

    On the first worker failure the remaining workers are cancelled (so
    downstream DROP SCHEMA cleanup isn't blocked on their locks) and that
    original exception propagates.
    """
    if not _SNAPSHOT_TOKEN_RE.match(snapshot_token):
        raise ValueError(f"Refusing unexpected snapshot token format: {snapshot_token!r}")

    def make_copy_task(table):
        def copy(cursor):
            cursor.execute("BEGIN")
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute(f"SET TRANSACTION SNAPSHOT '{snapshot_token}'")

            main_table = f'{quote_ident(main_schema)}.{quote_ident(table)}'
            schema_table = f'{quote_ident(schema)}.{quote_ident(table)}'
            logger.debug(f'Copying {main_table} -> {schema_table}')
            cursor.execute(f"INSERT INTO {schema_table} SELECT * FROM {main_table}")

            # Point the branch table's id default at main's sequence so all branches
            # continue to share a global id namespace (matches the previous behavior).
            # Schema-qualify the name: pg_get_serial_sequence resolves via search_path
            # and would silently return NULL for any non-search_path main_schema.
            cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [main_table])
            row = cursor.fetchone()
            if row and row[0]:
                cursor.execute(
                    f"ALTER TABLE {schema_table} ALTER COLUMN id SET DEFAULT nextval(%s)",
                    [row[0]],
                )

            cursor.execute("COMMIT")
        return copy

    _run_pool([make_copy_task(t) for t in tables], 'branch-copy', workers)


def parallel_build_indexes(
    index_tasks, schema, main_schema, workers, skip_indexes=()
):
    """
    Build indexes against the populated branch tables.

    ``index_tasks`` is an iterable of ``(tablename, indexname, indexdef)``
    tuples, typically derived from ``build_main_index_map()``. Each ``indexdef``
    is rewritten to point at the branch schema, then executed; the original
    index name is preserved so subsequent migrations see the same names that
    exist on main.

    Index names in ``skip_indexes`` are dropped — used to filter indexes that
    were removed in a later NetBox migration but may still exist on older
    installs, plus indexes that back constraints (those are created by
    ALTER TABLE ADD CONSTRAINT in ``parallel_add_constraints``).
    """
    skip = set(skip_indexes)
    tasks = [t for t in index_tasks if t[1] not in skip]
    if not tasks:
        return

    # pg_get_indexdef emits identifiers in canonical form: quoted when the name
    # is mixed-case, a reserved word, or contains special chars; bare otherwise.
    # quote_ident() gives us the exact same form, so the substring replacement
    # works regardless of how the operator named main_schema.
    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        cursor.execute(
            "SELECT quote_ident(%s), quote_ident(%s)", [main_schema, schema]
        )
        main_qident, schema_qident = cursor.fetchone()

    main_target = f' ON {main_qident}.'
    schema_replacement = f' ON {schema_qident}.'

    def make_build_task(item):
        _tablename, indexname, indexdef = item
        if main_target not in indexdef:
            # pg_get_indexdef normally emits " ON <schema>.<table>"; if the
            # substring is absent the format has shifted in a way we can't
            # safely rewrite. Fail loudly rather than silently dropping the
            # index — a missing PK/UNIQUE backing index on a branch would be
            # extremely hard to diagnose downstream.
            raise RuntimeError(
                f"Cannot rewrite indexdef for {indexname} to branch schema: "
                f"definition does not contain {main_target!r}: {indexdef!r}"
            )
        # Only the table reference carries the ` ON <schema>.` prefix, so this
        # rewrites exactly that and nothing else. A schema-qualified function in an
        # expression index (e.g. `... (public.func(col))`) is intentionally left
        # pointing at main_schema: branch schemas hold only tables, never functions,
        # so the function lives in main and must continue to be referenced there.
        new_def = indexdef.replace(main_target, schema_replacement, 1)

        def build(cursor):
            logger.debug(f'Creating index {schema}.{indexname}')
            cursor.execute(new_def)
        return build

    _run_pool([make_build_task(t) for t in tasks], 'branch-index', workers)


def parallel_add_constraints(constraint_tasks, schema, workers):
    """
    Add PRIMARY KEY / UNIQUE / EXCLUDE constraints to the populated branch
    tables via ``ALTER TABLE ... ADD CONSTRAINT name <condef>``. PostgreSQL
    builds each constraint's backing index implicitly under the constraint's
    name, so the resulting catalog state matches what `LIKE INCLUDING INDEXES`
    produced previously.

    ``constraint_tasks`` is an iterable of ``(tablename, conname, condef)``
    tuples, typically derived from ``build_main_constraint_map()``.
    """
    def make_add_task(item):
        tablename, conname, condef = item
        # Always-quote identifiers here — condef comes from pg_get_constraintdef
        # which already quotes column references where needed, so we only need
        # to handle the bare schema/table/constraint names we inject ourselves.
        sql = (
            f'ALTER TABLE {quote_ident(schema)}.{quote_ident(tablename)} '
            f'ADD CONSTRAINT {quote_ident(conname)} {condef}'
        )

        def add(cursor):
            logger.debug(f'Adding constraint {conname} on {schema}.{tablename}')
            cursor.execute(sql)
        return add

    _run_pool([make_add_task(t) for t in constraint_tasks], 'branch-constraint', workers)


def parallel_analyze_tables(tables, schema, workers):
    """
    Run ANALYZE on the freshly-populated branch tables across a worker pool.

    After the Phase 2 bulk ``INSERT INTO branch.t SELECT * FROM main.t`` the branch
    tables carry no planner statistics until autovacuum eventually reaches them, so
    the planner falls back to default (essentially empty-table) estimates. The first
    queries against a new branch — sync, change-diff computation, and so on — can then
    pick sequential scans over index scans and run far slower than expected. An explicit
    ANALYZE is much cheaper than the provision itself and removes that cold-start
    penalty. It must run after Phase 3 so expression-index statistics are collected too.

    ANALYZE is statistics-only and never affects correctness, so each table's ANALYZE
    swallows its own error rather than propagating: a single table's failure must not
    trip ``_run_pool``'s fail-fast cancellation and leave every *other* table without
    statistics (the exact cold-start regression this pass exists to prevent). Callers
    should likewise treat any error that still escapes (e.g. worker connection setup)
    as non-fatal.
    """
    def make_analyze_task(table):
        sql_text = f'ANALYZE {quote_ident(schema)}.{quote_ident(table)}'

        def analyze(cursor):
            try:
                logger.debug(f'Analyzing {schema}.{table}')
                cursor.execute(sql_text)
            except Exception:
                # Per-table best-effort: don't let one table abort the pool. (RQ's
                # job timeout is raised in the main thread, not here, so this cannot
                # mask a job cancellation.)
                logger.warning(
                    f'ANALYZE of {schema}.{table} failed; leaving its statistics to autovacuum.',
                    exc_info=True,
                )
        return analyze

    _run_pool([make_analyze_task(t) for t in tables], 'branch-analyze', workers)
