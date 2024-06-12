from django import forms
from django.utils.translation import gettext as _

from extras.choices import ObjectChangeActionChoices
from core.models import ObjectType
from netbox.forms import NetBoxModelFilterSetForm
from utilities.forms.fields import ContentTypeMultipleChoiceField, DynamicModelMultipleChoiceField, TagFilterField
from utilities.forms.rendering import FieldSet
from netbox_vcs.choices import ContextStatusChoices
from netbox_vcs.models import ChangeDiff, Context

__all__ = (
    'ChangeDiffFilterForm',
    'ContextFilterForm',
)


class ContextFilterForm(NetBoxModelFilterSetForm):
    model = Context
    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet('status', 'last_sync', name=_('Context')),
    )
    status = forms.MultipleChoiceField(
        label=_('Status'),
        choices=ContextStatusChoices,
        required=False
    )
    tag = TagFilterField(model)


class ChangeDiffFilterForm(NetBoxModelFilterSetForm):
    model = ChangeDiff
    fieldsets = (
        FieldSet('filter_id',),
        FieldSet('context_id', 'object_type_id', 'action', name=_('Change')),
    )
    context_id = DynamicModelMultipleChoiceField(
        queryset=Context.objects.all(),
        required=False,
        label=_('Context')
    )
    object_type_id = ContentTypeMultipleChoiceField(
        queryset=ObjectType.objects.with_feature('change_logging'),
        required=False,
        label=_('Object Type'),
    )
    action = forms.MultipleChoiceField(
        label=_('Action'),
        choices=ObjectChangeActionChoices,
        required=False
    )
