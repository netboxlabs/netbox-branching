from django import forms

from utilities.forms import ConfirmationForm

__all__ = (
    'MergeBranchForm',
    'SyncBranchForm',
)


class SyncBranchForm(ConfirmationForm):
    commit = forms.BooleanField(
        required=False
    )


class MergeBranchForm(ConfirmationForm):
    commit = forms.BooleanField(
        required=False
    )
