import uuid
from datetime import timedelta

from core.choices import ObjectChangeActionChoices
from dcim.models import Site
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections, transaction
from django.db.models.signals import post_save
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from netbox.context_managers import event_tracking

from netbox_branching.models import Branch, ChangeDiff
from netbox_branching.tests.utils import provision_branch
from netbox_branching.utilities import activate_branch

User = get_user_model()

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


class MainSideConflictTestCase(TransactionTestCase):
    """
    Conflicts must be recorded when the change in main arrives *after* the branch's
    change. Previously the global-change path in record_change_diff() refreshed
    ChangeDiff.current with a queryset update(), which bypasses save() and therefore
    _update_conflicts(), leaving the stored conflicts stale (usually NULL). Every
    consumer of the field was affected, including the API's 409 conflict gate and the
    merge form's acknowledgement check.

    The ordering matters: when main changes first, the branch's own change re-saves the
    ChangeDiff and recomputes conflicts as a side effect, which masked the bug.
    """

    serialized_rollback = True

    def setUp(self):
        self.user = User.objects.create_user(username='testuser')
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user
        self.request = request

    def tearDown(self):
        for branch in Branch.objects.all():
            if hasattr(connections._connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _get_diff(self, branch, site_id):
        """Fetch the ChangeDiff straight from the database, without re-saving it."""
        return ChangeDiff.objects.get(
            branch=branch,
            object_type=ContentType.objects.get_for_model(Site),
            object_id=site_id,
        )

    def test_conflict_recorded_for_main_change_after_branch_change(self):
        with event_tracking(self.request):
            site = Site.objects.create(name='Site 1', slug='site-1', description='original')
        site_id = site.pk

        branch = provision_branch(user=self.user, name='Branch 1')

        # Branch changes the description first
        with activate_branch(branch), event_tracking(self.request):
            branch_site = Site.objects.get(pk=site_id)
            branch_site.snapshot()
            branch_site.description = 'branch-desc'
            branch_site.save()

        self.assertIsNone(self._get_diff(branch, site_id).conflicts)

        # Main then changes the same field to a different value
        with event_tracking(self.request):
            main_site = Site.objects.get(pk=site_id)
            main_site.snapshot()
            main_site.description = 'main-desc'
            main_site.save()

        diff = self._get_diff(branch, site_id)
        self.assertEqual(diff.current['description'], 'main-desc')
        self.assertEqual(diff.conflicts, ['description'])

    def test_no_conflict_for_main_change_to_unrelated_field(self):
        with event_tracking(self.request):
            site = Site.objects.create(
                name='Site 2', slug='site-2', description='original', status='active'
            )
        site_id = site.pk

        branch = provision_branch(user=self.user, name='Branch 2')

        with activate_branch(branch), event_tracking(self.request):
            branch_site = Site.objects.get(pk=site_id)
            branch_site.snapshot()
            branch_site.description = 'branch-desc'
            branch_site.save()

        # Main changes a field the branch never touched
        with event_tracking(self.request):
            main_site = Site.objects.get(pk=site_id)
            main_site.snapshot()
            main_site.status = 'staging'
            main_site.save()

        diff = self._get_diff(branch, site_id)
        self.assertEqual(diff.current['status'], 'staging')
        self.assertIsNone(diff.conflicts)

    def test_conflict_cleared_when_main_matches_branch_value(self):
        """
        A conflict recorded from an earlier change in main must also be cleared once a
        later change in main converges on the branch's value.
        """
        with event_tracking(self.request):
            site = Site.objects.create(name='Site 3', slug='site-3', description='original')
        site_id = site.pk

        branch = provision_branch(user=self.user, name='Branch 3')

        with activate_branch(branch), event_tracking(self.request):
            branch_site = Site.objects.get(pk=site_id)
            branch_site.snapshot()
            branch_site.description = 'agreed-desc'
            branch_site.save()

        with event_tracking(self.request):
            main_site = Site.objects.get(pk=site_id)
            main_site.snapshot()
            main_site.description = 'main-desc'
            main_site.save()

        self.assertEqual(self._get_diff(branch, site_id).conflicts, ['description'])

        # Main converges on the branch's value; the conflict is no longer real
        with event_tracking(self.request):
            main_site = Site.objects.get(pk=site_id)
            main_site.snapshot()
            main_site.description = 'agreed-desc'
            main_site.save()

        self.assertIsNone(self._get_diff(branch, site_id).conflicts)

    def test_object_repr_reflects_branch_after_main_change(self):
        """
        Saving the ChangeDiff to recompute conflicts must not overwrite object_repr with
        main's representation: it records the branch's view of the object.
        """
        with event_tracking(self.request):
            site = Site.objects.create(name='Original Name', slug='site-4')
        site_id = site.pk

        branch = provision_branch(user=self.user, name='Branch 4')

        with activate_branch(branch), event_tracking(self.request):
            branch_site = Site.objects.get(pk=site_id)
            branch_site.snapshot()
            branch_site.name = 'Branch Name'
            branch_site.save()

        self.assertEqual(self._get_diff(branch, site_id).object_repr, 'Branch Name')

        with event_tracking(self.request):
            main_site = Site.objects.get(pk=site_id)
            main_site.snapshot()
            main_site.description = 'changed in main'
            main_site.save()

        self.assertEqual(self._get_diff(branch, site_id).object_repr, 'Branch Name')

    def test_conflict_recorded_when_main_deletes_object(self):
        """
        The branch-UPDATE + main-DELETE combination previously needed its own
        recompute loop; it must keep working now that every diff is saved individually.
        """
        with event_tracking(self.request):
            site = Site.objects.create(name='Site 5', slug='site-5', description='original')
        site_id = site.pk

        branch = provision_branch(user=self.user, name='Branch 5')

        with activate_branch(branch), event_tracking(self.request):
            branch_site = Site.objects.get(pk=site_id)
            branch_site.snapshot()
            branch_site.description = 'branch-desc'
            branch_site.save()

        with event_tracking(self.request):
            Site.objects.get(pk=site_id).delete()

        diff = self._get_diff(branch, site_id)
        self.assertIsNone(diff.current)
        self.assertEqual(diff.conflicts, ['description'])

    def test_deleted_diff_does_not_abort_main_write(self):
        """
        A ChangeDiff removed (e.g. by branch deprovisioning) between the receiver's
        SELECT and its save() must not abort the enclosing write to main.
        """
        with event_tracking(self.request):
            site = Site.objects.create(name='Site 6', slug='site-6', description='original')
        site_id = site.pk

        branch_1 = provision_branch(user=self.user, name='Branch 6a')
        branch_2 = provision_branch(user=self.user, name='Branch 6b')
        for branch in (branch_1, branch_2):
            with activate_branch(branch), event_tracking(self.request):
                branch_site = Site.objects.get(pk=site_id)
                branch_site.snapshot()
                branch_site.description = f'{branch.name} desc'
                branch_site.save()
        self.assertEqual(ChangeDiff.objects.filter(object_id=site_id).count(), 2)

        def delete_sibling_diffs(sender, instance, **kwargs):
            # Stand in for the branch being deprovisioned mid-loop: the receiver has already
            # selected both diffs, so the second save() will match zero rows.
            ChangeDiff.objects.filter(object_id=site_id).exclude(pk=instance.pk).delete()

        post_save.connect(delete_sibling_diffs, sender=ChangeDiff)
        try:
            # The atomic() block stands in for the view's transaction, which is what makes an
            # escaping exception poison the connection rather than merely propagate.
            with transaction.atomic():
                with event_tracking(self.request):
                    main_site = Site.objects.get(pk=site_id)
                    main_site.snapshot()
                    main_site.description = 'changed in main'
                    main_site.save()
                # The transaction must still be usable after the swallowed error.
                self.assertTrue(Site.objects.filter(pk=site_id).exists())
        finally:
            post_save.disconnect(delete_sibling_diffs, sender=ChangeDiff)

        self.assertEqual(Site.objects.get(pk=site_id).description, 'changed in main')


class DivergentKeySetTestCase(TestCase):
    """
    original is captured from the branch's first change while modified is refreshed on every
    subsequent one, so a NetBox upgrade that adds or renames a field with no registered migrator
    can leave their key sets divergent. A key missing from modified must not raise KeyError: the
    global-change path now reaches this code, where the exception would propagate out of the
    post_save receiver and roll back an unrelated write to main.
    """

    def _diff(self, **kwargs):
        return ChangeDiff(
            object_type=ContentType.objects.get_for_model(Site),
            object_id=1,
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            **kwargs,
        )

    def test_key_missing_from_modified_with_object_deleted_in_main(self):
        diff = self._diff(
            original={'name': 'foo', 'retired': 'x'},
            modified={'name': 'foo'},
            current=None,
        )
        diff._update_conflicts()
        self.assertEqual(diff.conflicts, ['retired'])

    def test_key_missing_from_modified_with_object_present_in_main(self):
        diff = self._diff(
            original={'name': 'foo', 'retired': 'x'},
            modified={'name': 'foo'},
            current={'name': 'foo', 'retired': 'y'},
        )
        diff._update_conflicts()
        self.assertEqual(diff.conflicts, ['retired'])
