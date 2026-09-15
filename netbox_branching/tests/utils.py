import logging
from collections import namedtuple
from typing import ClassVar

from django.core.management import call_command
from django.core.management.sql import emit_post_migrate_signal
from django.db import DatabaseError, connections
from django.test import TransactionTestCase
from netbox.plugins import get_plugin_config

from netbox_branching.models import Branch
from netbox_branching.provisioning import quote_ident

__all__ = (
    'FastTeardownTransactionTestCase',
    'fetchall',
    'fetchone',
    'provision_branch',
)

logger = logging.getLogger('netbox_branching.tests')


def provision_branch(*, user, name='Test Branch', **kwargs):
    """
    Create and provision a Branch, returning it with status READY.

    Branch.provision() runs synchronously in the calling thread; it updates
    the row via Branch.objects.filter(pk=...).update(...), which bypasses
    the in-memory instance, so the caller needs refresh_from_db() to see
    the post-provision status.

    Any extra kwargs (e.g. merge_strategy) are passed through to the Branch
    constructor.
    """
    branch = Branch(name=name, **kwargs)
    branch.save(provision=False)
    branch.provision(user=user)
    branch.refresh_from_db()
    return branch


def fetchall(cursor):
    """
    Map cursor.fetchall() into a list of named tuples for convenience.
    """
    result = namedtuple('Result', [col[0] for col in cursor.description])
    return [
        result(*row) for row in cursor.fetchall()
    ]


def fetchone(cursor):
    """
    Map cursor.fetchone() into a named tuple for convenience.
    """
    if ret := cursor.fetchone():
        result = namedtuple('Result', [col[0] for col in cursor.description])
        return result(*ret)
    return None


class FastTeardownTransactionTestCase(TransactionTestCase):
    """
    TransactionTestCase whose teardown empties only the tables that hold rows.

    Django's flush TRUNCATEs every table in the database. TRUNCATE rewrites a
    relfilenode for each table *and* each of its indexes — roughly 1,450 files on a
    stock NetBox schema — no matter how few rows the test actually wrote, which makes
    teardown cost the same for a test that created three objects as for one that
    created three thousand. Probing for the non-empty tables in a single round trip
    and DELETE-ing just those takes the same teardown from ~1.7s to ~0.02s.

    Django already flushes with reset_sequences=False, so switching TRUNCATE for
    DELETE does not change sequence behaviour. If the database user may not set
    session_replication_role (it requires superuser), this falls back to Django's
    own flush.

    serialized_rollback keeps working: Django restores the serialized migration data
    in _fixture_setup, which this class does not override. Only the flush leg is
    replaced, and the post_migrate signal Django's flush would have emitted is
    emitted here under the same condition Django uses.
    """
    # Cached per connection alias: the probe is built from the table list, which
    # cannot change while the suite runs.
    _nonempty_probe: ClassVar[dict] = {}

    def _fixture_teardown(self):
        # Fall back per alias rather than for all of them: deferring to
        # super()._fixture_teardown() would re-flush aliases this loop had already
        # emptied, TRUNCATE-ing them a second time for nothing.
        for db_name in self._databases_names(include_mirrors=False):
            if self._fast_flush(db_name):
                # Django's flush emits post_migrate unless inhibited, which is what
                # recreates ContentType and Permission rows it just deleted. Every
                # subclass today sets serialized_rollback, so Django inhibits the
                # signal and restores that data in _fixture_setup instead — but a
                # future subclass that does not would silently lose those rows here
                # without this.
                if not self._inhibit_post_migrate(db_name):
                    emit_post_migrate_signal(verbosity=0, interactive=False, db=db_name)
            else:
                self._django_flush(db_name)
            self._drop_branch_schemas(db_name)

    def _drop_branch_schemas(self, db_name):
        """Drop any branch schemas the test left behind.

        Deleting a Branch through the ORM deprovisions its schema, but the flush above
        removes the rows directly, so every branch a test provisions leaks its schema.
        Roughly 150 tables and 1,000 indexes each, which accumulate in pg_class for the
        rest of the run and make every subsequent provision slower — a full suite leaks
        ~25,000 relations, and a --keepdb database keeps them across runs.

        Anything still matching the prefix at teardown is by definition left over, so
        this also clears orphans stranded by earlier interrupted runs.
        """
        prefix = get_plugin_config('netbox_branching', 'schema_prefix')
        connection = connections[db_name]
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT nspname FROM pg_namespace WHERE nspname LIKE %s',
                    [f'{prefix}%'],
                )
                schemas = [row[0] for row in cursor.fetchall()]
                if schemas:
                    cursor.execute('; '.join(
                        f'DROP SCHEMA {quote_ident(s)} CASCADE' for s in schemas
                    ))
        except DatabaseError:
            logger.warning('Failed to drop leftover branch schemas', exc_info=True)

    def _inhibit_post_migrate(self, db_name):
        """Whether Django would suppress post_migrate for this alias after a flush."""
        return self.available_apps is not None or (
            self.serialized_rollback
            and hasattr(connections[db_name], '_test_serialized_contents')
        )

    def _django_flush(self, db_name):
        """Flush one alias exactly as TransactionTestCase._fixture_teardown would.

        Mirrors Django's own call so the fallback path stays faithful to it; if
        Django changes the arguments it passes to `flush`, this needs to follow.
        """
        call_command(
            'flush',
            verbosity=0,
            interactive=False,
            database=db_name,
            reset_sequences=False,
            allow_cascade=self.available_apps is not None,
            inhibit_post_migrate=self._inhibit_post_migrate(db_name),
        )

    def _fast_flush(self, db_name):
        """Empty the non-empty tables on `db_name`. Returns False to defer to Django."""
        connection = connections[db_name]
        probe = self._nonempty_probe.get(db_name)
        if probe is None:
            tables = connection.introspection.django_table_names(
                only_existing=True, include_views=False
            )
            if not tables:
                return True
            probe = ' UNION ALL '.join(
                f"SELECT '{table}' AS t WHERE EXISTS "
                f"(SELECT 1 FROM {connection.ops.quote_name(table)} LIMIT 1)"
                for table in tables
            )
            self._nonempty_probe[db_name] = probe

        try:
            with connection.cursor() as cursor:
                cursor.execute(probe)
                nonempty = [row[0] for row in cursor.fetchall()]
                if not nonempty:
                    return True
                # Deleting in FK order would mean topologically sorting the whole
                # schema on every teardown; disabling FK enforcement for the delete
                # is equivalent here because every referencing row is being removed
                # too.
                cursor.execute('SET session_replication_role = replica')
                try:
                    cursor.execute('; '.join(
                        f'DELETE FROM {connection.ops.quote_name(table)}'
                        for table in nonempty
                    ))
                finally:
                    cursor.execute('SET session_replication_role = DEFAULT')
        except DatabaseError:
            logger.warning(
                'Fast teardown failed; falling back to Django flush. This is expected '
                'if the database user is not a superuser.',
                exc_info=True,
            )
            return False
        return True
