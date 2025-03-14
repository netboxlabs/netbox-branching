import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import MAIN_SCHEMA
from netbox_branching.models import Branch
from netbox_branching.utilities import get_tables_to_replicate, activate_branch
from .utils import fetchall, fetchone
from dcim.models import Site, Device, DeviceRole, Manufacturer, DeviceType


class BranchTestCase(TransactionTestCase):
    serialized_rollback = True

    def test_create_branch(self):
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)

        tables_to_replicate = get_tables_to_replicate()

        with connection.cursor() as cursor:

            # Check that the schema was created in the database
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [branch.schema_name]
            )
            row = cursor.fetchone()
            self.assertIsNotNone(row)

            # Check that all expected tables exist in the schema
            cursor.execute(
                "SELECT * FROM information_schema.tables WHERE table_schema=%s",
                [branch.schema_name]
            )
            tables_expected = {*tables_to_replicate, 'core_objectchange'}
            tables_found = {row.table_name for row in fetchall(cursor)}
            self.assertSetEqual(tables_expected, tables_found)

            # Check that object counts match the main schema for each table
            for table_name in tables_to_replicate:
                cursor.execute(f"SELECT COUNT(id) FROM {MAIN_SCHEMA}.{table_name}")
                main_count = fetchone(cursor).count
                cursor.execute(f"SELECT COUNT(id) FROM {branch.schema_name}.{table_name}")
                branch_count = fetchone(cursor).count
                self.assertEqual(
                    main_count,
                    branch_count,
                    msg=f"Table {table_name} object count differs from main schema"
                )

    def test_delete_branch(self):
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)
        branch.delete()

        with connection.cursor() as cursor:

            # Check that the schema no longer exists in the database
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [branch.schema_name]
            )
            row = fetchone(cursor)
            self.assertIsNone(row)

    def test_branch_schema_id(self):
        branch = Branch(name='Branch 1')
        self.assertIsNotNone(branch.schema_id, msg="Schema ID has not been set")
        self.assertIsNotNone(re.match(r'^[a-z0-9]{8}', branch.schema_id), msg="Schema ID does not conform")
        schema_id = branch.schema_id

        branch.save(provision=False)
        branch.refresh_from_db()
        self.assertEqual(branch.schema_id, schema_id, msg="Schema ID was changed during save()")

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'max_working_branches': 2,
            'job_timeout': 300,
        }
    })
    def test_max_working_branches(self):
        """
        Verify that the max_working_branches config parameter is enforced.
        """
        Branch.objects.bulk_create((
            Branch(name='Branch 1', status=BranchStatusChoices.MERGED),
            Branch(name='Branch 2', status=BranchStatusChoices.READY),
        ))

        # Second active branch should be permitted (merged branches don't count)
        branch = Branch(name='Branch 3')
        branch.full_clean()
        branch.save()

        # Attempting to create a third active branch should fail
        branch = Branch(name='Branch 4')
        with self.assertRaises(ValidationError):
            branch.full_clean()

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'max_branches': 2,
            'job_timeout': 300,
        }
    })
    def test_max_branches(self):
        """
        Verify that the max_branches config parameter is enforced.
        """
        Branch.objects.bulk_create((
            Branch(name='Branch 1', status=BranchStatusChoices.ARCHIVED),
            Branch(name='Branch 2', status=BranchStatusChoices.READY),
        ))

        # Creating a second non-archived Branch should succeed
        branch = Branch(name='Branch 3')
        branch.full_clean()
        branch.save(provision=False)

        # Creating a third non-archived Branch should fail
        branch = Branch(name='Branch 4')
        with self.assertRaises(ValidationError):
            branch.full_clean()

    @override_settings(CHANGELOG_RETENTION=10)
    def test_is_stale(self):
        branch = Branch(name='Branch 1')
        branch.save(provision=False)

        # Set creation time to 9 days in the past
        branch.last_sync = timezone.now() - timedelta(days=9)
        branch.save()
        self.assertFalse(branch.is_stale)

        # Set creation time to 11 days in the past
        branch.last_sync = timezone.now() - timedelta(days=11)
        branch.save()
        self.assertTrue(branch.is_stale)

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'max_branches': 32,
            'job_timeout': 0,
            'job_timeout_modifier': {
                "default_create": 1,  # seconds
                "default_update": 2,  # seconds
                "default_delete": 4,  # seconds
                "dcim.device": {
                    "create": 8,  # seconds
                    "update": 16,  # seconds
                    "delete": 32,  # seconds
                }
            },
        }
    })
    def test_branch_timeout(self):
        site_a, _ = Site.objects.get_or_create(name="Site A",
                                               slug="site_a",
                                               description="site_a_description")
        device_manufacturer, _ = Manufacturer.objects.get_or_create(name="Device Manufacturer",
                                                                    slug="device_manufacturer")
        device_role, _ = DeviceRole.objects.get_or_create(name="Device Role",
                                                          slug="device_role")
        device_role_existing, _ = DeviceRole.objects.get_or_create(name="Device Role Existing",
                                                                   slug="device_role_existing")
        device_type, _ = DeviceType.objects.get_or_create(manufacturer=device_manufacturer,
                                                          model="Device Model",
                                                          slug="device_model")
        device_existing, _ = Device.objects.get_or_create(name="Device Existing",
                                                          site=site_a,
                                                          role=device_role,
                                                          device_type=device_type)

        with self.subTest("Create a device role with default timeout"):
            branch = Branch(name='Branch Device Role Create')
            branch.full_clean()
            branch.save(provision=False)
            branch.refresh_from_db()
            branch.provision(user=None)
            with activate_branch(branch):
                device_role_create, _ = DeviceRole.objects.get_or_create(name="Device Role Create",
                                                                         slug="device_role_create")
            self.assertEqual(branch.job_timeout, 1)

        with self.subTest("Update a device role with default timeout"):
            branch = Branch(name='Branch Role Update')
            branch.full_clean()
            branch.save(provision=False)
            branch.refresh_from_db()
            branch.provision(user=None)
            with activate_branch(branch):
                device_role_existing.name = "Device Role Update"
                device_role_existing.save()
            self.assertEqual(branch.job_timeout, 2)

        with self.subTest("Delete a device role with default timeout"):
            branch = Branch(name='Branch Role Delete')
            branch.full_clean()
            branch.save(provision=False)
            branch.refresh_from_db()
            branch.provision(user=None)
            with activate_branch(branch):
                device_role_existing.delete()
            self.assertEqual(branch.job_timeout, 4)

        with self.subTest("Create a device"):
            branch = Branch(name='Branch Device Create')
            branch.full_clean()
            branch.save(provision=False)
            branch.refresh_from_db()
            branch.provision(user=None)
            with activate_branch(branch):
                device_create, _ = Device.objects.get_or_create(name="Device Create",
                                                                site=site_a,
                                                                role=device_role,
                                                                device_type=device_type)
            self.assertEqual(branch.job_timeout, 8)

        with self.subTest("Update a device"):
            branch = Branch(name='Branch Device Update')
            branch.full_clean()
            branch.save(provision=False)
            branch.refresh_from_db()
            branch.provision(user=None)
            with activate_branch(branch):
                device_existing.name = "Device Update"
                device_existing.save()
            self.assertEqual(branch.job_timeout, 16)

        with self.subTest("Delete a device"):
            branch = Branch(name='Branch Device Delete')
            branch.full_clean()
            branch.save(provision=False)
            branch.refresh_from_db()
            branch.provision(user=None)
            with activate_branch(branch):
                device_existing.delete()
            self.assertEqual(branch.job_timeout, 32)
