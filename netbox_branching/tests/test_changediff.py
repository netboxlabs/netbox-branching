from django.test import TestCase
from django.contrib.contenttypes.models import ContentType

from core.choices import ObjectChangeActionChoices
from netbox_branching.models import Branch, ChangeDiff


class ChangeDiffTestCase(TestCase):
    """
    Test cases for ChangeDiff model diff properties.
    """

    def setUp(self):
        self.branch = Branch.objects.create(name='Test Branch')
        self.content_type = ContentType.objects.get_for_model(Branch)

    def test_diff_properties_with_create_action(self):
        """
        Test that diff properties handle CREATE action where original=None and current=None.
        Fixes issue #428.
        """
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=1,
            object_repr='Test CREATE',
            action=ObjectChangeActionChoices.ACTION_CREATE,
            original=None,
            modified={'id': 1, 'name': 'new_object'},
            current=None,
        )

        # These should not raise AttributeError
        self.assertEqual(change_diff.original_diff, {})
        self.assertIsInstance(change_diff.modified_diff, dict)
        self.assertEqual(change_diff.current_diff, {})
