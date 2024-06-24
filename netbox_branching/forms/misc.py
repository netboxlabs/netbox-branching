from django import forms

__all__ = (
    'MergeBranchForm',
    'SyncBranchForm',
)


class SyncBranchForm(forms.Form):
    commit = forms.BooleanField(
        required=False,
        initial=True
    )


class MergeBranchForm(forms.Form):
    commit = forms.BooleanField(
        required=False,
        initial=True
    )
