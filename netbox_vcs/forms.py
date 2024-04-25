from django import forms

from netbox.forms import NetBoxModelForm
from utilities.forms import ConfirmationForm
from utilities.forms.rendering import FieldSet

from .models import Context


class ContextForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'description'),
    )

    class Meta:
        model = Context
        fields = (
            'name', 'description',
        )


class ApplyContextForm(ConfirmationForm):
    commit = forms.BooleanField(
        required=False
    )
