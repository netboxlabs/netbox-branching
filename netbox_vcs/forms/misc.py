from django import forms

from utilities.forms import ConfirmationForm

__all__ = (
    'ApplyBranchForm',
    'SyncBranchForm',
)


class SyncBranchForm(ConfirmationForm):
    commit = forms.BooleanField(
        required=False
    )


class ApplyBranchForm(ConfirmationForm):
    commit = forms.BooleanField(
        required=False
    )
