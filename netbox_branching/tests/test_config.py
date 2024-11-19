from django.test import TransactionTestCase, override_settings

from netbox.registry import registry
from netbox_branching.models import Branch
from netbox_branching.utilities import register_models


class ConfigTestCase(TransactionTestCase):
    serialized_rollback = True

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'exempt_models': ['ipam.prefix'],
        }
    })
    def test_exempt_models(self):
        register_models()
        self.assertNotIn('prefix', registry['model_features']['branching']['ipam'])

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'schema_prefix': 'dummy_',
        }
    })
    def test_schema_prefix(self):
        branch = Branch(name='Branch 5')
        self.assertEqual(branch.schema_name, f'dummy_{branch.schema_id}')
