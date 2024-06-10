from django.db import connections
from django.test import TransactionTestCase

from dcim.models import DeviceRole, Site
from netbox_vcs.models import Context

from netbox_vcs.utilities import activate_context


class QueryTestCase(TransactionTestCase):
    serialized_rollback = True

    def tearDown(self):
        # Manually tear down the dynamic connection created for the Context
        context = Context.objects.first()
        connections[context.connection_name].close()

    def test_query(self):
        Site.objects.create(name='Site 1', slug='site-1')
        DeviceRole.objects.create(name='Device role 1', slug='device-role-1')

        context = Context(name='Context1')
        context.schema_id = 'test1234'
        context.save()

        # Query for the objects in the primary schema
        self.assertEqual(Site.objects.count(), 1)
        self.assertEqual(DeviceRole.objects.count(), 1)

        # Query for the objects using the context
        self.assertEqual(Site.objects.using(context.connection_name).count(), 1)
        self.assertEqual(DeviceRole.objects.using(context.connection_name).count(), 1)
        with activate_context(context):
            self.assertEqual(Site.objects.count(), 1)
            self.assertEqual(DeviceRole.objects.count(), 1)
