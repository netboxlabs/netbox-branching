from django.test import TestCase, override_settings
from ipam.models import Prefix

from netbox_branching.utilities import DynamicSchemaDict, supports_branching


class ConfigTestCase(TestCase):
    # Pure in-memory checks (no schema DDL), so TestCase is sufficient.

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'exempt_models': ['ipam.prefix'],
        }
    })
    def test_exempt_models(self):
        self.assertFalse(supports_branching(Prefix))


class DynamicSchemaDictTestCase(TestCase):

    def test_preserves_database_options(self):
        databases = DynamicSchemaDict({
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': 'netbox',
                'OPTIONS': {
                    'sslmode': 'require',
                    'connect_timeout': 10,
                }
            }
        })

        branch_config = databases['schema_test123']
        self.assertEqual(branch_config, {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'netbox',
            'OPTIONS': {
                'sslmode': 'require',
                'connect_timeout': 10,
                'options': '-c search_path=test123,public'
            }
        })
