from django import forms
from django.utils.translation import gettext_lazy as _

from netbox_branching.models import ChangeDiff

__all__ = (
    'BranchActionForm',
    'ConfirmationForm',
    'MigrateBranchForm',
)


class BranchActionForm(forms.Form):
    pk = forms.ModelMultipleChoiceField(
        queryset=ChangeDiff.objects.all(),
        required=False
    )
    commit = forms.BooleanField(
        required=False,
        label=_('Commit changes'),
        help_text=_('Leave unchecked to perform a dry run')
    )
    collapse_changes = forms.BooleanField(
        required=False,
        initial=False,
        label=_('Collapse Object Changes'),
        help_text=_('Use the collapsed merge strategy')
    )

    def __init__(self, branch, *args, allow_commit=True, action=None, **kwargs):
        self.branch = branch
        super().__init__(*args, **kwargs)

        if not allow_commit:
            self.fields['commit'].disabled = True

        # Only show collapse_changes for merge operations, not revert
        if action == 'revert':
            del self.fields['collapse_changes']

    def clean(self):
        super().clean()

        # Verify that any ChangeDiffs which have conflicts have been acknowledged
        conflicted_diffs = ChangeDiff.objects.filter(
            branch=self.branch,
            conflicts__isnull=False
        )
        selected_diffs = self.cleaned_data.get('pk', list())
        if conflicted_diffs and not set(conflicted_diffs).issubset(selected_diffs):
            raise forms.ValidationError(_("All conflicts must be acknowledged in order to merge the branch."))

        return self.cleaned_data


class ConfirmationForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label=_('Confirm')
    )


class MigrateBranchForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label=_('Confirm migrations'),
        help_text=_(
            'All migrations will be applied in order. <strong>Migrations cannot be reversed once applied.</strong>'
        )
    )
