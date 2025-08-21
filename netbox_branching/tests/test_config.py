from django.test import TransactionTestCase, override_settings

from ipam.models import Prefix
from netbox_branching.models import Branch
from netbox_branching.utilities import supports_branching


class ConfigTestCase(TransactionTestCase):
    serialized_rollback = True

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'exempt_models': ['ipam.prefix'],
        }
    })
    def test_exempt_models(self):
        self.assertFalse(supports_branching(Prefix))

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'schema_prefix': 'dummy_',
        }
    })
    def test_schema_prefix(self):
        branch = Branch(name='Branch 5')
        self.assertEqual(branch.schema_name, f'dummy_{branch.schema_id}')
