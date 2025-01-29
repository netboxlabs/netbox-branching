from django import forms
from django.utils.translation import gettext_lazy as _

from netbox_branching.models import Branch, ChangeDiff

__all__ = (
    'BranchActionForm',
    'BranchPullForm',
    'ConfirmationForm',
)


class BranchActionForm(forms.Form):
    pk = forms.ModelMultipleChoiceField(
        queryset=ChangeDiff.objects.all(),
        required=False,
        widget=forms.HiddenInput()
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


class BranchPullForm(BranchActionForm):
    source = forms.ModelChoiceField(
        queryset=Branch.objects.all()
    )
    # start = forms.ModelChoiceField(
    #     queryset=ObjectChange.objects.all(),
    #     required=False
    # )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['source'].queryset = Branch.objects.exclude(pk=self.branch.pk)
        # self.fields['start'].queryset = self.branch.get_replay_queue()


class ConfirmationForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label=_('Confirm')
    )
