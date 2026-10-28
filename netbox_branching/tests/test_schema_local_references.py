"""
Tests for ``strip_schema_local_references``, which keeps schema-local row IDs out of replayed CREATEs. (#684)
"""

from django.test import SimpleTestCase

from dcim.models import CableTermination, Interface
from netbox_branching.utilities import strip_schema_local_references


class StripSchemaLocalReferencesTestCase(SimpleTestCase):
    def test_strips_fk_to_schema_local_model(self):
        data = {'device': 1, 'name': 'eth0', '_name': '9999999999999999eth000000000000000', '_site': 2, '_path': 3}

        stripped = strip_schema_local_references(Interface, data)

        self.assertNotIn('_path', stripped)
        # Other private fields hold values a raw save would not recompute, so they must survive
        self.assertEqual(stripped, {k: v for k, v in data.items() if k != '_path'})
        self.assertIn('_path', data, 'input data must not be modified')

    def test_keeps_fks_to_shared_and_change_logged_models(self):
        data = {'cable': 1, 'cable_end': 'A', 'termination_type': 2, 'termination_id': 3, '_device': 4}

        self.assertEqual(strip_schema_local_references(CableTermination, data), data)
