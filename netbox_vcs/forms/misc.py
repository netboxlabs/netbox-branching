from django import forms

from utilities.forms import ConfirmationForm

__all__ = (
    'ApplyContextForm',
    'SyncContextForm',
)


class SyncContextForm(ConfirmationForm):
    commit = forms.BooleanField(
        required=False
    )


class ApplyContextForm(ConfirmationForm):
    commit = forms.BooleanField(
        required=False
    )
