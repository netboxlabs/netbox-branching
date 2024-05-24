from django.test import TestCase

from dcim.models import DeviceRole, Site
from netbox_vcs.models import Context


class QueryTestCase(TestCase):
    databases = {'default', 'schema_ctx_test1234'}

    @classmethod
    def setUpTestData(cls):
        from django.conf import settings
        print(repr(settings.DATABASES))

        Site.objects.create(name='Site 1', slug='site-1')
        DeviceRole.objects.create(name='Device role 1', slug='device-role-1')

        context = Context(name='Context1')
        context.schema_id = 'test1234'
        context.save()

    # TODO: This test is known to be broken. Further investigation is needed to determine
    # how to facilitate dynamic database connection in Django's testing environment.
    def test_query(self):
        context = Context.objects.first()

        # Query for the objects in the primary schema
        self.assertEqual(Site.objects.using('default').count(), 1)
        self.assertEqual(DeviceRole.objects.using('default').count(), 1)

        # Query for the objects using the context
        self.assertEqual(Site.objects.using(context.connection_name).count(), 1)
        self.assertEqual(DeviceRole.objects.using(context.connection_name).count(), 1)
