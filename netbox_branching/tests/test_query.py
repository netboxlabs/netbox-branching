from django.db import connections
from django.test import TransactionTestCase

from dcim.models import DeviceRole, Site
from netbox_branching.models import Branch

from netbox_branching.utilities import activate_branch


class QueryTestCase(TransactionTestCase):
    serialized_rollback = True

    def tearDown(self):
        # Manually tear down the dynamic connection created for the Branch
        branch = Branch.objects.first()
        connections[branch.connection_name].close()

    def test_query(self):
        Site.objects.create(name='Site 1', slug='site-1')
        DeviceRole.objects.create(name='Device role 1', slug='device-role-1')

        branch = Branch(name='Branch 1')
        branch.schema_id = 'test1234'
        branch.save(provision=False)
        branch.provision()

        # Query for the objects in the main schema
        self.assertEqual(Site.objects.count(), 1)
        self.assertEqual(DeviceRole.objects.count(), 1)

        # Query for the objects using the branch
        self.assertEqual(Site.objects.using(branch.connection_name).count(), 1)
        self.assertEqual(DeviceRole.objects.using(branch.connection_name).count(), 1)
        with activate_branch(branch):
            self.assertEqual(Site.objects.count(), 1)
            self.assertEqual(DeviceRole.objects.count(), 1)

        branch.deprovision()
