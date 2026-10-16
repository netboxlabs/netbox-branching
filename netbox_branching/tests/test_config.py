from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from ipam.models import Prefix

from netbox_branching import migrations
from netbox_branching.backends import SchemaBranchingBackend
from netbox_branching.migrations._utilities import get_schema_config
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


class MigrationSchemaConfigTestCase(SimpleTestCase):
    """
    The data migrations read the schema configuration through a helper of their own rather
    than through SchemaBranchingBackend, so that changing the backend can never break an
    upgrade from v1.x. The helper must nonetheless resolve each parameter exactly as the
    backend does today.
    """

    def assertMatchesBackend(self, name, expected):
        self.assertEqual(get_schema_config(name), expected)
        self.assertEqual(SchemaBranchingBackend().get_config(name), expected)

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {}})
    def test_defaults(self):
        self.assertMatchesBackend('schema_prefix', 'branch_')
        self.assertMatchesBackend('main_schema', 'public')

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {'schema_prefix': 'legacy_'}})
    def test_legacy_root_level_parameter(self):
        self.assertMatchesBackend('schema_prefix', 'legacy_')

    @override_settings(PLUGINS_CONFIG={'netbox_branching': {
        'schema_prefix': 'legacy_',
        'backend_config': {'schema_prefix': 'current_'},
    }})
    def test_backend_config_takes_precedence(self):
        self.assertMatchesBackend('schema_prefix', 'current_')

    def test_migrations_do_not_import_the_backends(self):
        for path in Path(migrations.__file__).parent.glob('*.py'):
            with self.subTest(migration=path.name):
                self.assertNotIn('netbox_branching.backends', path.read_text())


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
