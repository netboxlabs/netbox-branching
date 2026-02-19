from itertools import chain

import django_filters
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db.models import ForeignKey, ManyToManyField, ManyToManyRel, ManyToOneRel, OneToOneRel
from django.test import TestCase
from django.utils.module_loading import import_string

try:
    from taggit.managers import TaggableManager
except ImportError:
    TaggableManager = None

from core.choices import ObjectChangeActionChoices
from netbox_branching.choices import BranchEventTypeChoices, BranchStatusChoices
from netbox_branching.filtersets import BranchEventFilterSet, BranchFilterSet, ChangeDiffFilterSet
from netbox_branching.models import Branch, BranchEvent, ChangeDiff


EXEMPT_MODEL_FIELDS = (
    'comments',
    'custom_field_data',
    'level',    # MPTT fields
    'lft',
    'rght',
    'tree_id',
)


class BaseFilterSetTests:
    """
    Mixin that adds test_missing_filters: asserts every model field has a
    corresponding filter defined on its FilterSet.  Fields that are
    intentionally not filterable should be listed in ignore_fields.
    """
    ignore_fields = tuple()

    def _get_filters_for_field(self, field):
        """
        Return a list of (filter_name, expected_filter_class_or_None) tuples
        that should exist on the FilterSet for the given model field.
        """
        # ForeignKey / OneToOneRel
        if issubclass(field.__class__, ForeignKey) or type(field) is OneToOneRel:
            # ContentType FKs (used as part of a GFK) are exempt
            if field.related_model is ContentType:
                return [(None, None)]
            return [(f'{field.name}_id', django_filters.ModelMultipleChoiceFilter)]

        # Many-to-many (forward & reverse)
        if type(field) in (ManyToManyField, ManyToManyRel):
            if field.related_model is ContentType:
                return [
                    ('object_type', None),
                    ('object_type_id', django_filters.ModelMultipleChoiceFilter),
                ]
            related_name = field.related_model._meta.verbose_name.lower().replace(' ', '_')
            return [(f'{related_name}_id', django_filters.ModelMultipleChoiceFilter)]

        # Tags
        if TaggableManager is not None and type(field) is TaggableManager:
            return [('tag', None)]

        # All other fields – just check presence, not class
        return [(field.name, None)]

    def test_missing_filters(self):
        """
        Check that every model field (not in ignore_fields) has a corresponding
        filter defined on the FilterSet.
        """
        app_label = self.__class__.__module__.split('.')[0]
        model = self.queryset.model
        model_name = model.__name__

        filterset = import_string(f'{app_label}.filtersets.{model_name}FilterSet')
        self.assertEqual(model, filterset.Meta.model, 'FilterSet model does not match!')

        defined_filters = filterset.get_filters()

        for model_field in model._meta.get_fields():

            # Skip private fields
            if model_field.name.startswith('_'):
                continue

            # Skip exempted and intentionally-ignored fields
            if model_field.name in chain(self.ignore_fields, EXEMPT_MODEL_FIELDS):
                continue

            # Reverse FK relations don't need filters
            if type(model_field) is ManyToOneRel:
                continue

            # Generic relationships don't need filters
            if type(model_field) in (GenericForeignKey, GenericRelation):
                continue

            for filter_name, filter_class in self._get_filters_for_field(model_field):
                if filter_name is None:
                    continue

                self.assertIn(
                    filter_name,
                    defined_filters.keys(),
                    f'No filter defined for {filter_name} ({model_field.name})!',
                )

                if filter_class is not None:
                    self.assertIsInstance(
                        defined_filters[filter_name],
                        filter_class,
                        f'Invalid filter class for {filter_name} (expected {filter_class})!',
                    )


class BranchFilterSetTestCase(TestCase, BaseFilterSetTests):
    queryset = Branch.objects.all()
    filterset = BranchFilterSet

    # Fields intentionally absent from BranchFilterSet
    ignore_fields = (
        'owner',
        'schema_id',
        'applied_migrations',
        'merged_time',
        'merged_by',
        'merge_strategy',
    )

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


class BranchEventFilterSetTestCase(TestCase, BaseFilterSetTests):
    queryset = BranchEvent.objects.all()
    filterset = BranchEventFilterSet

    # branch and user have no filters on BranchEventFilterSet
    ignore_fields = ('branch', 'user')

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


class ChangeDiffFilterSetTestCase(TestCase, BaseFilterSetTests):
    queryset = ChangeDiff.objects.all()
    filterset = ChangeDiffFilterSet

    # These fields have no direct filters; object_repr is searched via q,
    # conflicts is handled by has_conflicts, json fields are not filtered.
    ignore_fields = ('object_repr', 'original', 'modified', 'current', 'conflicts')

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
