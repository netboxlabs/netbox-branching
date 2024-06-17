from django import forms
from django.utils.translation import gettext as _

from core.choices import ObjectChangeActionChoices
from core.models import ObjectType
from netbox.forms import NetBoxModelFilterSetForm
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import ChangeDiff, Branch
from utilities.forms.fields import ContentTypeMultipleChoiceField, DynamicModelMultipleChoiceField, TagFilterField
from utilities.forms.rendering import FieldSet

__all__ = (
    'ChangeDiffFilterForm',
    'BranchFilterForm',
)


class BranchFilterForm(NetBoxModelFilterSetForm):
    model = Branch
    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet('status', 'last_sync', name=_('Branch')),
    )
    status = forms.MultipleChoiceField(
        label=_('Status'),
        choices=BranchStatusChoices,
        required=False
    )
    tag = TagFilterField(model)


class ChangeDiffFilterForm(NetBoxModelFilterSetForm):
    model = ChangeDiff
    fieldsets = (
        FieldSet('filter_id',),
        FieldSet('branch_id', 'object_type_id', 'action', name=_('Change')),
    )
    branch_id = DynamicModelMultipleChoiceField(
        queryset=Branch.objects.all(),
        required=False,
        label=_('Branch')
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
