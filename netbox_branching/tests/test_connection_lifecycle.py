import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import tag, TransactionTestCase

from netbox_branching.models import Branch
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
