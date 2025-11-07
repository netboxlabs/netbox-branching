from django.test import TestCase
from django.contrib.contenttypes.models import ContentType

from core.choices import ObjectChangeActionChoices
from netbox_branching.models import Branch, ChangeDiff


class ChangeDiffTestCase(TestCase):
    """
    Test cases for ChangeDiff model, specifically the _update_conflicts() method.
    """

    def setUp(self):
        """Set up test branch and content type."""
        self.branch = Branch.objects.create(name='Test Branch')
        # Use a generic content type for testing
        self.content_type = ContentType.objects.get_for_model(Branch)

    def test_update_conflicts_with_none_modified(self):
        """
        Test that _update_conflicts() handles None modified data gracefully.
        Fixes issue #355: AttributeError when self.modified is None.
        """
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=1,
            object_repr='Test Object',
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            original={'field1': 'value1', 'field2': 'value2'},
            modified=None,  # This causes the AttributeError in issue #355
            current={'field1': 'value1', 'field2': 'value3'}
        )

        # Should not raise AttributeError
        try:
            change_diff.save()
            # Conflicts should be None when modified is None
            self.assertIsNone(change_diff.conflicts)
        except AttributeError as e:
            self.fail(f'_update_conflicts() raised AttributeError with None modified: {e}')

    def test_update_conflicts_with_none_original(self):
        """
        Test that _update_conflicts() handles None original data gracefully.
        This was partially fixed in PR #350.
        """
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=1,
            object_repr='Test Object',
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            original=None,
            modified={'field1': 'value1', 'field2': 'value2'},
            current={'field1': 'value1', 'field2': 'value3'}
        )

        # Should not raise AttributeError
        try:
            change_diff.save()
            self.assertIsNone(change_diff.conflicts)
        except AttributeError as e:
            self.fail(f'_update_conflicts() raised AttributeError with None original: {e}')

    def test_update_conflicts_with_none_current(self):
        """
        Test that _update_conflicts() handles None current data gracefully.
        This was partially fixed in PR #350.
        """
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=1,
            object_repr='Test Object',
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            original={'field1': 'value1', 'field2': 'value2'},
            modified={'field1': 'value1', 'field2': 'value3'},
            current=None
        )

        # Should not raise AttributeError
        try:
            change_diff.save()
            self.assertIsNone(change_diff.conflicts)
        except AttributeError as e:
            self.fail(f'_update_conflicts() raised AttributeError with None current: {e}')

    def test_update_conflicts_with_valid_data_no_conflicts(self):
        """
        Test that _update_conflicts() correctly identifies no conflicts.
        """
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=1,
            object_repr='Test Object',
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            original={'field1': 'value1', 'field2': 'value2'},
            modified={'field1': 'value1', 'field2': 'value3'},
            current={'field1': 'value1', 'field2': 'value2'}
        )

        change_diff.save()
        # No conflicts: field2 changed in modified but not in current
        self.assertIsNone(change_diff.conflicts)

    def test_update_conflicts_with_valid_data_has_conflicts(self):
        """
        Test that _update_conflicts() correctly identifies conflicts.
        """
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=1,
            object_repr='Test Object',
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            original={'field1': 'value1', 'field2': 'value2'},
            modified={'field1': 'value_branch', 'field2': 'value2'},
            current={'field1': 'value_main', 'field2': 'value2'}
        )

        change_diff.save()
        # Conflict: field1 changed differently in both branch and main
        self.assertIsNotNone(change_diff.conflicts)
        self.assertIn('field1', change_diff.conflicts)

    def test_update_conflicts_with_missing_key_in_modified(self):
        """
        Test that _update_conflicts() handles missing keys in modified data.
        Ensures 'k in self.modified' check prevents KeyError.
        """
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=1,
            object_repr='Test Object',
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            original={'field1': 'value1', 'field2': 'value2'},
            modified={'field1': 'value1'},  # field2 missing
            current={'field1': 'value1', 'field2': 'value3'}
        )

        # Should not raise KeyError
        try:
            change_diff.save()
            # Should complete without error
            self.assertIsNone(change_diff.conflicts)
        except KeyError as e:
            self.fail(f'_update_conflicts() raised KeyError with missing key: {e}')

    def test_delete_conflicts_with_none_original(self):
        """
        Test DELETE action with None original data.
        """
        change_diff = ChangeDiff(
            branch=self.branch,
            object_type=self.content_type,
            object_id=1,
            object_repr='Test Object',
            action=ObjectChangeActionChoices.ACTION_DELETE,
            original=None,
            modified=None,
            current={'field1': 'value1'}
        )

        try:
            change_diff.save()
            self.assertIsNone(change_diff.conflicts)
        except AttributeError as e:
            self.fail(f'_update_conflicts() raised AttributeError on DELETE with None original: {e}')
