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

__all__ = (
    'build_main_constraint_map',
    'build_main_index_map',
    'parallel_add_constraints',
    'parallel_build_indexes',
    'parallel_copy_tables',
)

logger = logging.getLogger('netbox_branching.branch.provision')

# pg_export_snapshot() returns a token of the form "<digits>-<digits>-<digits>"
# (sometimes with hex chars). We validate before interpolating because SET TRANSACTION
# SNAPSHOT is a utility statement that does not accept bind parameters in all drivers.
_SNAPSHOT_TOKEN_RE = re.compile(r'\A[A-Fa-f0-9\-]+\Z')


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
    conn = connections.create_connection(DEFAULT_DB_ALIAS)
    try:
        with conn.cursor() as cursor:
            for pid in pids:
                try:
                    cursor.execute("SELECT pg_cancel_backend(%s)", [pid])
                except Exception:
                    logger.exception(f"Failed to cancel worker backend {pid}")
    except Exception:
        logger.exception("Failed to issue worker cancellation")
    finally:
        try:
            conn.close()
        except Exception:
            logger.debug("Ignoring error while closing cancellation connection", exc_info=True)


def _run_pool(items, worker_fn, label, workers):
    """Run ``worker_fn(item, register_pid, cancel_event)`` across a thread pool.

    On the first worker exception: signal cancel_event, cancel pending futures,
    pg_cancel_backend any in-flight workers' queries, drain the remaining
    futures, then re-raise the original exception. ``register_pid`` is a callback
    workers invoke once they have a connection so the cancellation pass knows
    which backends to terminate.
    """
    workers = max(1, int(workers))
    pids = []
    pids_lock = threading.Lock()
    cancel_event = threading.Event()

    def register_pid(pid):
        with pids_lock:
            pids.append(pid)

    def wrapped(item):
        return worker_fn(item, register_pid, cancel_event)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=label) as pool:
        futures = {pool.submit(wrapped, item): item for item in items}
        first_error = None
        for future in as_completed(futures):
            item = futures[future]
            try:
                future.result()
            except Exception as e:
                if first_error is None:
                    first_error = e
                    logger.exception(f"{label} worker failed on {item!r}")
                    cancel_event.set()
                    for f in futures:
                        f.cancel()
                    with pids_lock:
                        to_cancel = list(pids)
                    _cancel_backends(to_cancel)
                # Continue draining so the pool exits cleanly; subsequent errors
                # are from cancellations and don't override the first cause.
        if first_error is not None:
            raise first_error


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

    def _copy_one(table, register_pid, cancel_event):
        if cancel_event.is_set():
            return
        conn = connections[DEFAULT_DB_ALIAS]
        # ThreadPoolExecutor reuses threads across tasks; force a fresh backend
        # so each task starts with a clean transaction state.
        conn.close()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                register_pid(cursor.fetchone()[0])
                if cancel_event.is_set():
                    return

                cursor.execute("BEGIN")
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute(f"SET TRANSACTION SNAPSHOT '{snapshot_token}'")

                main_table = f'{main_schema}.{table}'
                schema_table = f'{schema}.{table}'
                logger.debug(f'Copying {main_table} -> {schema_table}')
                cursor.execute(f"INSERT INTO {schema_table} SELECT * FROM {main_table}")

                # Point the branch table's id default at main's sequence so all branches
                # continue to share a global id namespace (matches the previous behavior).
                cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [table])
                row = cursor.fetchone()
                if row and row[0]:
                    cursor.execute(
                        f"ALTER TABLE {schema_table} ALTER COLUMN id SET DEFAULT nextval(%s)",
                        [row[0]],
                    )

                cursor.execute("COMMIT")
        finally:
            conn.close()

    _run_pool(list(tables), _copy_one, 'branch-copy', workers)


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

    def _build_one(item, register_pid, cancel_event):
        if cancel_event.is_set():
            return
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
        new_def = indexdef.replace(main_target, schema_replacement, 1)

        conn = connections[DEFAULT_DB_ALIAS]
        conn.close()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                register_pid(cursor.fetchone()[0])
                if cancel_event.is_set():
                    return
                logger.debug(f'Creating index {schema}.{indexname}')
                cursor.execute(new_def)
        finally:
            conn.close()

    _run_pool(tasks, _build_one, 'branch-index', workers)


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
    tasks = list(constraint_tasks)
    if not tasks:
        return

    def _add_one(item, register_pid, cancel_event):
        if cancel_event.is_set():
            return
        tablename, conname, condef = item
        conn = connections[DEFAULT_DB_ALIAS]
        conn.close()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                register_pid(cursor.fetchone()[0])
                if cancel_event.is_set():
                    return
                sql = (
                    f'ALTER TABLE {schema}.{tablename} '
                    f'ADD CONSTRAINT {conname} {condef}'
                )
                logger.debug(f'Adding constraint {conname} on {schema}.{tablename}')
                cursor.execute(sql)
        finally:
            conn.close()

    _run_pool(tasks, _add_one, 'branch-constraint', workers)
