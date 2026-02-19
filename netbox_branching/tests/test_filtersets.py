from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from core.choices import ObjectChangeActionChoices
from netbox_branching.choices import BranchEventTypeChoices, BranchStatusChoices
from netbox_branching.filtersets import BranchEventFilterSet, BranchFilterSet, ChangeDiffFilterSet
from netbox_branching.models import Branch, BranchEvent, ChangeDiff


class BranchFilterSetTestCase(TestCase):
    queryset = Branch.objects.all()
    filterset = BranchFilterSet

    @classmethod
    def setUpTestData(cls):
        branches = (
            Branch(name='Branch 1', description='foobar1'),
            Branch(name='Branch 2', description='foobar2'),
            Branch(name='Branch 3', description='foobar3'),
        )
        for branch in branches:
            branch.save(provision=False)

        Branch.objects.filter(name='Branch 1').update(status=BranchStatusChoices.READY)
        Branch.objects.filter(name='Branch 2').update(status=BranchStatusChoices.MERGED)
        # Branch 3 remains NEW

    def test_id(self):
        params = {'id': [b.pk for b in Branch.objects.all()[:2]]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_name(self):
        params = {'name': ['Branch 1', 'Branch 2']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_description(self):
        params = {'description': ['foobar1', 'foobar2']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_status(self):
        params = {'status': [BranchStatusChoices.READY, BranchStatusChoices.MERGED]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_q_name(self):
        params = {'q': 'Branch 1'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_q_description(self):
        params = {'q': 'foobar2'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)


class BranchEventFilterSetTestCase(TestCase):
    queryset = BranchEvent.objects.all()
    filterset = BranchEventFilterSet

    @classmethod
    def setUpTestData(cls):
        branches = (
            Branch(name='Branch 1'),
            Branch(name='Branch 2'),
        )
        for branch in branches:
            branch.save(provision=False)

        BranchEvent.objects.create(branch=branches[0], type=BranchEventTypeChoices.PROVISIONED)
        BranchEvent.objects.create(branch=branches[0], type=BranchEventTypeChoices.SYNCED)
        BranchEvent.objects.create(branch=branches[1], type=BranchEventTypeChoices.MERGED)

    def test_id(self):
        params = {'id': [e.pk for e in BranchEvent.objects.all()[:2]]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_type(self):
        params = {'type': [BranchEventTypeChoices.PROVISIONED, BranchEventTypeChoices.SYNCED]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)


class ChangeDiffFilterSetTestCase(TestCase):
    queryset = ChangeDiff.objects.all()
    filterset = ChangeDiffFilterSet

    @classmethod
    def setUpTestData(cls):
        branches = (
            Branch(name='Branch 1'),
            Branch(name='Branch 2'),
        )
        for branch in branches:
            branch.save(provision=False)

        ct = ContentType.objects.get_for_model(Branch)

        # Two diffs on Branch 1, one pointing to each branch object.
        # One diff on Branch 2, also pointing to Branch 1's object.
        # This gives us two diffs with object_repr='Branch 1' for q-search testing.
        ChangeDiff.objects.create(
            branch=branches[0],
            object_type=ct,
            object_id=branches[0].pk,
            action=ObjectChangeActionChoices.ACTION_CREATE,
        )
        ChangeDiff.objects.create(
            branch=branches[0],
            object_type=ct,
            object_id=branches[1].pk,
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            conflicts=['name'],
        )
        ChangeDiff.objects.create(
            branch=branches[1],
            object_type=ct,
            object_id=branches[0].pk,
            action=ObjectChangeActionChoices.ACTION_DELETE,
        )

    def test_id(self):
        params = {'id': [cd.pk for cd in ChangeDiff.objects.all()[:2]]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_q(self):
        # object_repr is set to str(branch) = branch.name on save()
        # Two diffs reference branches[0] as their object (object_id=branches[0].pk)
        branch = Branch.objects.get(name='Branch 1')
        params = {'q': branch.name}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_branch_id(self):
        branch = Branch.objects.get(name='Branch 1')
        params = {'branch_id': [branch.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_branch(self):
        branch = Branch.objects.get(name='Branch 1')
        params = {'branch': [branch.schema_id]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_object_type_id(self):
        ct = ContentType.objects.get_for_model(Branch)
        params = {'object_type_id': [ct.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)

    def test_object_type(self):
        params = {'object_type': 'netbox_branching.branch'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)

    def test_object_id(self):
        branch = Branch.objects.get(name='Branch 1')
        params = {'object_id': [branch.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_action(self):
        params = {'action': [ObjectChangeActionChoices.ACTION_CREATE, ObjectChangeActionChoices.ACTION_UPDATE]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_has_conflicts_true(self):
        params = {'has_conflicts': True}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_has_conflicts_false(self):
        params = {'has_conflicts': False}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
