"""
Helpers for parallelizing the data-copy and index-build passes of Branch.provision().

The single-process approach used previously is fine for small databases but becomes
the dominant cost on installs with multi-GB branchable data. This module splits the
two heaviest phases across a thread pool, using PostgreSQL's exported snapshot
mechanism (the same one pg_dump --jobs uses) so that every worker reads main from
an identical MVCC view.
"""
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import DEFAULT_DB_ALIAS, connections

__all__ = (
    'build_main_index_map',
    'parallel_build_indexes',
    'parallel_copy_tables',
)

logger = logging.getLogger('netbox_branching.branch.provision')

# pg_export_snapshot() returns a token of the form "<digits>-<digits>-<digits>"
# (sometimes with hex chars). We validate before interpolating because SET TRANSACTION
# SNAPSHOT is a utility statement that does not accept bind parameters in all drivers.
_SNAPSHOT_TOKEN_RE = re.compile(r'\A[A-Fa-f0-9\-]+\Z')


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

    Any worker exception propagates after the pool drains.
    """
    if not _SNAPSHOT_TOKEN_RE.match(snapshot_token):
        raise ValueError(f"Refusing unexpected snapshot token format: {snapshot_token!r}")

    workers = max(1, int(workers))

    def _copy_one(table):
        conn = connections[DEFAULT_DB_ALIAS]
        # ThreadPoolExecutor reuses threads across tasks; force a fresh backend
        # so each task starts with a clean transaction state.
        conn.close()
        try:
            with conn.cursor() as cursor:
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

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='branch-copy') as pool:
        futures = {pool.submit(_copy_one, table): table for table in tables}
        for future in as_completed(futures):
            table = futures[future]
            try:
                future.result()
            except Exception:
                logger.exception(f"Worker failed while copying {table}")
                raise


def parallel_build_indexes(index_tasks, schema, main_schema, workers, skip_indexes=()):
    """
    Build indexes against the populated branch tables.

    ``index_tasks`` is an iterable of ``(tablename, indexname, indexdef)``
    tuples, typically derived from ``build_main_index_map()``. Each ``indexdef``
    is rewritten to point at the branch schema, then executed; the original
    index name is preserved so subsequent migrations see the same names that
    exist on main.

    Index names in ``skip_indexes`` are dropped — used to filter indexes that
    were removed in a later NetBox migration but may still exist on older
    installs.
    """
    workers = max(1, int(workers))
    skip = set(skip_indexes)

    main_prefix = f' ON {main_schema}.'
    schema_prefix = f' ON {schema}.'

    def _build_one(item):
        tablename, indexname, indexdef = item
        target = f'{main_prefix}{tablename}'
        if target not in indexdef:
            logger.warning(
                f"indexdef for {indexname} does not reference {target!r}; skipping"
            )
            return
        new_def = indexdef.replace(target, f'{schema_prefix}{tablename}', 1)

        conn = connections[DEFAULT_DB_ALIAS]
        conn.close()
        try:
            with conn.cursor() as cursor:
                logger.debug(f'Creating index {schema}.{indexname}')
                cursor.execute(new_def)
        finally:
            conn.close()

    tasks = [t for t in index_tasks if t[1] not in skip]

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='branch-index') as pool:
        futures = {pool.submit(_build_one, task): task for task in tasks}
        for future in as_completed(futures):
            _, indexname, _ = futures[future]
            try:
                future.result()
            except Exception:
                logger.exception(f"Worker failed while building index {indexname}")
                raise
