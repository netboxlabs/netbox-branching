import django_filters
from django.db.models import Q
from django.utils.translation import gettext as _

from core.models import ObjectType
from extras.choices import ObjectChangeActionChoices
from netbox.filtersets import BaseFilterSet, NetBoxModelFilterSet
from utilities import filters
from .choices import *
from .models import *

__all__ = (
    'ContextFilterSet',
    'ChangeDiffFilterSet',
)


class ContextFilterSet(NetBoxModelFilterSet):
    status = django_filters.MultipleChoiceFilter(
        choices=ContextStatusChoices,
        null_value=None
    )
    last_sync = filters.MultiValueDateTimeFilter()

    class Meta:
        model = Context
        fields = ('id', 'name', 'description')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) |
            Q(description__icontains=value)
        )


class ChangeDiffFilterSet(BaseFilterSet):
    context_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Context.objects.all(),
        label=_('Context (ID)'),
    )
    context = django_filters.ModelMultipleChoiceFilter(
        field_name='context__schema_id',
        queryset=Context.objects.all(),
        to_field_name='schema_id',
        label=_('Context (schema ID)'),
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

    class Meta:
        model = ChangeDiff
        fields = ('id', 'object_type', 'object_id')
