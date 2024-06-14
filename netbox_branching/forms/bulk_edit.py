from django import forms
from django.utils.translation import gettext_lazy as _
from netbox_branching.models import Branch

from netbox.forms import NetBoxModelBulkEditForm
from utilities.forms.fields import CommentField
from utilities.forms.rendering import FieldSet

__all__ = (
    'BranchBulkEditForm',
)


class BranchBulkEditForm(NetBoxModelBulkEditForm):
    description = forms.CharField(
        label=_('Description'),
        max_length=200,
        required=False
    )
    comments = CommentField()

    model = Branch
    fieldsets = (
        FieldSet('description',),
    )
    nullable_fields = ('description', 'comments')
