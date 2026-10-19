import time

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connections
from django.test import TestCase, TransactionTestCase, tag

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import Branch
from netbox_branching.signal_receivers import check_pending_migrations
from netbox_branching.utilities import activate_branch, close_old_branch_connections


@tag('regression')  # netbox-branching #358
class BranchConnectionLifecycleTestCase(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        """Set up test environment with CONN_MAX_AGE=1."""
        self.original_max_age = settings.DATABASES['default'].get('CONN_MAX_AGE', 0)
        settings.DATABASES['default']['CONN_MAX_AGE'] = 1
        self.user = get_user_model().objects.create_user(username='testuser', is_superuser=True)
        self.branches = []

    def tearDown(self):
        """Clean up branches and restore CONN_MAX_AGE."""
        for branch in self.branches:
            try:
                connections[branch.connection_name].close()
            except Exception:
                pass
            Branch.objects.filter(pk=branch.pk).delete()
        settings.DATABASES['default']['CONN_MAX_AGE'] = self.original_max_age

    def create_and_provision_branch(self, name):
        """Create and provision a test branch."""
        branch = Branch(name=name, description=f'Test {name}')
        branch.save(provision=False)
        branch.provision(self.user)
        self.branches.append(branch)
        return branch

    def open_branch_connection(self, branch):
        """Open a connection to the branch by executing a query."""
        with activate_branch(branch):
            from django.contrib.contenttypes.models import ContentType
            list(ContentType.objects.using(branch.connection_name).all()[:1])

    def test_branch_connections_close_after_max_age(self):
        """Branch connections should close after CONN_MAX_AGE expires."""
        branch = self.create_and_provision_branch('test-conn-cleanup')
        self.open_branch_connection(branch)

        conn = connections[branch.connection_name]
        self.assertIsNotNone(conn.connection, "Connection should be open after query")
        self.assertIsNotNone(conn.close_at, "close_at should be set when CONN_MAX_AGE > 0")

        time.sleep(2)
        close_old_branch_connections()

        self.assertIsNone(conn.connection, "Connection should be closed after CONN_MAX_AGE expires")

    def test_multiple_branch_connections_cleanup(self):
        """Multiple branch connections should all close after CONN_MAX_AGE."""
        branches = [self.create_and_provision_branch(f'test-multi-{i}') for i in range(3)]

        for branch in branches:
            self.open_branch_connection(branch)

        conns = [connections[b.connection_name] for b in branches]
        for conn in conns:
            self.assertIsNotNone(conn.connection, "Connection should be open")

        time.sleep(2)
        close_old_branch_connections()

        for i, conn in enumerate(conns):
            self.assertIsNone(conn.connection, f"Branch {i} connection should be closed")

    def test_check_pending_migrations_closes_branch_connections(self):
        """check_pending_migrations should close each branch's connection after inspecting it (#581)."""
        branches = [self.create_and_provision_branch(f'test-pending-{i}') for i in range(3)]

        # Open each branch connection so we can verify the sweep closes it.
        for branch in branches:
            self.open_branch_connection(branch)
        for branch in branches:
            self.assertIsNotNone(
                connections[branch.connection_name].connection, "Connection should be open before the sweep"
            )

        # Fire the post_migrate handler as Django would during `manage.py migrate`.
        check_pending_migrations(sender=apps.get_app_config('netbox_branching'), using=DEFAULT_DB_ALIAS)

        for branch in branches:
            self.assertIsNone(
                connections[branch.connection_name].connection,
                "Branch connection should be closed after check_pending_migrations",
            )

    def test_cleanup_handles_deleted_branch(self):
        """Cleanup should gracefully handle connections to deleted branch schemas."""
        branch = self.create_and_provision_branch('test-deleted-branch')
        self.open_branch_connection(branch)

        conn = connections[branch.connection_name]
        self.assertIsNotNone(conn.connection, "Connection should be open")

        branch.deprovision()
        Branch.objects.filter(pk=branch.pk).delete()
        self.branches.remove(branch)

        try:
            close_old_branch_connections()
        except Exception as e:
            self.fail(f"cleanup should not raise exception for deleted branch: {e}")


@tag('regression')  # netbox-branching #618
class CheckPendingMigrationsUnprovisionedBranchTestCase(TestCase):
    """
    A READY branch whose backend_id is NULL — reachable from a restored fixture or a manual
    status update — has no connection to close, and asking for its alias raises. Branch.
    pending_migrations guards against this, but the sweep's connection close sits in a finally
    outside that try/except: an exception escaping there aborts the whole `manage.py migrate`
    run, and masks anything the except had just logged.
    """

    def test_sweep_tolerates_an_unprovisioned_branch(self):
        branch = Branch(name='Never Provisioned')
        branch.save(provision=False)
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.READY)
        self.assertIsNone(Branch.objects.get(pk=branch.pk).backend_id)

        # Fire the post_migrate handler as Django would during `manage.py migrate`.
        check_pending_migrations(sender=apps.get_app_config('netbox_branching'), using=DEFAULT_DB_ALIAS)

        self.assertEqual(Branch.objects.get(pk=branch.pk).status, BranchStatusChoices.READY)
