from django import forms
from django.utils.translation import gettext_lazy as _

from netbox_branching.choices import BranchMergeStrategyChoices
from netbox_branching.models import ChangeDiff

__all__ = (
    'BranchSyncForm',
    'BranchMergeForm',
    'BranchRevertForm',
    'ConfirmationForm',
    'MigrateBranchForm',
)


class BaseBranchActionForm(forms.Form):
    """Base form for branch actions (sync, merge, revert)."""
    pk = forms.ModelMultipleChoiceField(
        queryset=ChangeDiff.objects.all(),
        required=False,
        widget=forms.MultipleHiddenInput()
    )
    commit = forms.BooleanField(
        required=False,
        label=_('Commit changes'),
        help_text=_('Leave unchecked to perform a dry run')
    )

    def __init__(self, branch, *args, allow_commit=True, **kwargs):
        self.branch = branch
        super().__init__(*args, **kwargs)

        if not allow_commit:
            self.fields['commit'].disabled = True

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


class BranchSyncForm(BaseBranchActionForm):
    """Form for syncing a branch."""
    pass


class BranchMergeForm(BaseBranchActionForm):
    """Form for merging a branch."""
    merge_strategy = forms.ChoiceField(
        choices=BranchMergeStrategyChoices,
        initial=BranchMergeStrategyChoices.ITERATIVE,
        required=True,
        label=_('Merge Strategy'),
        help_text=_('Strategy to use when merging changes.')
    )


class BranchRevertForm(BaseBranchActionForm):
    """Form for reverting a branch."""
    pass


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
