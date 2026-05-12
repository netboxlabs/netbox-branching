from datetime import timedelta

from core.choices import ObjectChangeActionChoices
from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase, TestCase

from netbox_branching.models import Branch, ChangeDiff

DATA_A = {'name': 'foo', 'description': ''}
DATA_B = {'name': 'foo', 'description': 'changed'}
DATA_C = {'name': 'foo', 'description': 'main change'}


def make_diff(**kwargs):
    """Return an un-saved ChangeDiff with only the JSON fields set."""
    return ChangeDiff(
        original=kwargs.get('original', DATA_A),
        modified=kwargs.get('modified', DATA_B),
        current=kwargs.get('current', None),
    )


class AlteredInModifiedTestCase(SimpleTestCase):

    def test_returns_changed_keys(self):
        diff = make_diff(original=DATA_A, modified=DATA_B)
        self.assertEqual(diff.altered_in_modified, {'description'})

    def test_no_changes(self):
        diff = make_diff(original=DATA_A, modified=DATA_A)
        self.assertEqual(diff.altered_in_modified, set())

    def test_original_none(self):
        # CREATE action — original is None
        diff = make_diff(original=None, modified=DATA_B)
        self.assertEqual(diff.altered_in_modified, set())

    def test_modified_none(self):
        # DELETE action — modified is None
        diff = make_diff(original=DATA_A, modified=None)
        self.assertEqual(diff.altered_in_modified, set())

    def test_both_none(self):
        diff = make_diff(original=None, modified=None)
        self.assertEqual(diff.altered_in_modified, set())


class AlteredInCurrentTestCase(SimpleTestCase):

    def test_returns_changed_keys(self):
        diff = make_diff(original=DATA_A, current=DATA_C)
        self.assertEqual(diff.altered_in_current, {'description'})

    def test_no_changes(self):
        diff = make_diff(original=DATA_A, current=DATA_A)
        self.assertEqual(diff.altered_in_current, set())

    def test_current_none(self):
        diff = make_diff(original=DATA_A, current=None)
        self.assertEqual(diff.altered_in_current, set())


class OriginalDiffTestCase(SimpleTestCase):

    def test_returns_altered_fields(self):
        diff = make_diff(original=DATA_A, modified=DATA_B)
        self.assertEqual(diff.original_diff, {'description': ''})

    def test_original_none(self):
        # CREATE action — original is None
        diff = make_diff(original=None, modified=DATA_B)
        self.assertEqual(diff.original_diff, {})

    def test_no_changes(self):
        diff = make_diff(original=DATA_A, modified=DATA_A)
        self.assertEqual(diff.original_diff, {})


class ModifiedDiffTestCase(SimpleTestCase):

    def test_returns_altered_fields(self):
        diff = make_diff(original=DATA_A, modified=DATA_B)
        self.assertEqual(diff.modified_diff, {'description': 'changed'})

    def test_modified_none(self):
        # DELETE action — modified is None
        diff = make_diff(original=DATA_A, modified=None)
        self.assertEqual(diff.modified_diff, {})

    def test_no_changes(self):
        diff = make_diff(original=DATA_A, modified=DATA_A)
        self.assertEqual(diff.modified_diff, {})


class CurrentDiffTestCase(SimpleTestCase):

    def test_returns_altered_fields(self):
        diff = make_diff(original=DATA_A, modified=DATA_B, current=DATA_C)
        self.assertEqual(diff.current_diff, {'description': 'main change'})

    def test_current_none(self):
        diff = make_diff(original=DATA_A, modified=DATA_B, current=None)
        self.assertEqual(diff.current_diff, {})

    def test_no_changes(self):
        diff = make_diff(original=DATA_A, modified=DATA_A, current=DATA_A)
        self.assertEqual(diff.current_diff, {})


class DiffPropertyTestCase(SimpleTestCase):
    """
    Verify the composite diff property doesn't raise for any None combination.
    """

    def test_create_action(self):
        # original=None, modified=data — CREATE
        diff = make_diff(original=None, modified=DATA_B)
        result = diff.diff
        self.assertEqual(result['original'], {})
        self.assertEqual(result['modified'], {})
        self.assertEqual(result['current'], {})

    def test_delete_action(self):
        # original=data, modified=None — DELETE
        diff = make_diff(original=DATA_A, modified=None)
        result = diff.diff
        self.assertEqual(result['original'], {})
        self.assertEqual(result['modified'], {})
        self.assertEqual(result['current'], {})

    def test_update_action(self):
        diff = make_diff(original=DATA_A, modified=DATA_B, current=DATA_C)
        result = diff.diff
        self.assertEqual(result['original'], {'description': ''})
        self.assertEqual(result['modified'], {'description': 'changed'})
        self.assertEqual(result['current'], {'description': 'main change'})


class LastUpdatedTestCase(TestCase):
    """
    Regression test for #483: last_updated must refresh on every save.
    """

    def test_last_updated_advances_on_save(self):
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        diff = ChangeDiff.objects.create(
            branch=branch,
            object_type=ContentType.objects.get_for_model(Branch),
            object_id=branch.pk,
            action=ObjectChangeActionChoices.ACTION_CREATE,
        )

        # Back-date via .update() (bypasses auto_now) so save() has room to advance.
        original = diff.last_updated - timedelta(hours=1)
        ChangeDiff.objects.filter(pk=diff.pk).update(last_updated=original)
        diff.refresh_from_db()
        self.assertEqual(diff.last_updated, original)

        diff.save()
        diff.refresh_from_db()
        self.assertGreater(diff.last_updated, original)
