from django.test import TestCase
from django.contrib.contenttypes.models import ContentType

from core.choices import ObjectChangeActionChoices
from dcim.models import Site
from netbox_branching.models import Branch, ChangeDiff


class ChangeDiffTestCase(TestCase):

    def setUp(self):
        self.branch = Branch.objects.create(name='Test Branch')
        self.content_type = ContentType.objects.get_for_model(Site)

    def test_create_action_none_handling(self):
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=1,
            object_repr='Test CREATE',
            action=ObjectChangeActionChoices.ACTION_CREATE,
            original=None,
            modified={'id': 1, 'name': 'new_object', 'status': 'active'},
            current=None,
        )

        self.assertEqual(change_diff.altered_in_modified, set())
        self.assertEqual(change_diff.altered_in_current, set())
        self.assertEqual(change_diff.altered_fields, [])
        self.assertEqual(change_diff.original_diff, {})
        self.assertEqual(change_diff.modified_diff, {})
        self.assertEqual(change_diff.current_diff, {})

    def test_delete_action_none_handling(self):
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=2,
            object_repr='Test DELETE',
            action=ObjectChangeActionChoices.ACTION_DELETE,
            original={'id': 2, 'name': 'deleted_object', 'status': 'active'},
            modified=None,
            current={'id': 2, 'name': 'deleted_object', 'status': 'active'},
        )

        self.assertEqual(change_diff.altered_in_modified, set())
        self.assertEqual(change_diff.altered_in_current, set())
        self.assertEqual(change_diff.altered_fields, [])
        self.assertEqual(change_diff.original_diff, {})
        self.assertEqual(change_diff.modified_diff, {})
        self.assertEqual(change_diff.current_diff, {})

    def test_update_action_current_none_handling(self):
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=3,
            object_repr='Test UPDATE no main',
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            original={'id': 3, 'name': 'original_name', 'status': 'active'},
            modified={'id': 3, 'name': 'modified_name', 'status': 'active'},
            current=None,
        )

        self.assertEqual(change_diff.altered_in_modified, {'name'})
        self.assertEqual(change_diff.altered_in_current, set())
        self.assertEqual(change_diff.altered_fields, ['name'])
        self.assertEqual(change_diff.original_diff, {'name': 'original_name'})
        self.assertEqual(change_diff.modified_diff, {'name': 'modified_name'})
        self.assertEqual(change_diff.current_diff, {})
