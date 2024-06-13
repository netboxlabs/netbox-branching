from django import forms
from django.utils.translation import gettext_lazy as _
from netbox_vcs.models import Context

from netbox.forms import NetBoxModelBulkEditForm
from utilities.forms.rendering import FieldSet

__all__ = (
    'ContextBulkEditForm',
)


class ContextBulkEditForm(NetBoxModelBulkEditForm):
    description = forms.CharField(
        label=_('Description'),
        max_length=200,
        required=False
    )

    model = Context
    fieldsets = (
        FieldSet('description',),
    )
    nullable_fields = (
        'description',
    )
