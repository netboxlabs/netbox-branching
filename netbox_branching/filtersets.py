import django_filters
from django.db.models import Q
from django.utils.translation import gettext as _

from core.choices import ObjectChangeActionChoices
from core.models import ObjectType
from netbox.filtersets import BaseFilterSet, NetBoxModelFilterSet
from utilities import filters
from .choices import *
from .models import *

__all__ = (
    'BranchEventFilterSet',
    'BranchFilterSet',
    'ChangeDiffFilterSet',
)


class BranchFilterSet(NetBoxModelFilterSet):
    status = django_filters.MultipleChoiceFilter(
        choices=BranchStatusChoices,
        null_value=None
    )
    last_sync = filters.MultiValueDateTimeFilter()

    class Meta:
        model = Branch
        fields = ('id', 'name', 'description')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) |
            Q(description__icontains=value)
        )


class BranchEventFilterSet(BaseFilterSet):
    type = django_filters.MultipleChoiceFilter(
        choices=BranchEventTypeChoices,
        null_value=None
    )
    time = filters.MultiValueDateTimeFilter()

    class Meta:
        model = BranchEvent
        fields = ('id',)


class ChangeDiffFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method='search',
        label=_('Search'),
    )
    branch_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Branch.objects.all(),
        label=_('Branch (ID)'),
    )
    branch = django_filters.ModelMultipleChoiceFilter(
        field_name='branch__schema_id',
        queryset=Branch.objects.all(),
        to_field_name='schema_id',
        label=_('Branch (schema ID)'),
    )
    last_updated = filters.MultiValueDateTimeFilter()
    object_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=ObjectType.objects.all(),
        field_name='object_type'
    )
    object_type = filters.ContentTypeFilter()
    action = django_filters.MultipleChoiceFilter(
        choices=ObjectChangeActionChoices,
        null_value=None
    )
    has_conflicts = django_filters.BooleanFilter(
        method='_has_conflicts'
    )

    class Meta:
        model = ChangeDiff
        fields = ('id', 'object_type', 'object_id')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(object_repr__icontains=value)
        )

    def _has_conflicts(self, queryset, name, value):
        if value:
            return queryset.filter(conflicts__isnull=False)
        return queryset.filter(conflicts__isnull=True)
