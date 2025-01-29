from django import forms
from django.utils.translation import gettext_lazy as _

from utilities.forms.utils import get_field_value
from utilities.forms.widgets import HTMXSelect
from netbox_branching.models import Branch, ChangeDiff, ObjectChange

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
        queryset=Branch.objects.all(),
        widget=HTMXSelect(
            attrs={
                'hx-target': 'body'
            }
        )
    )
    atomic = forms.BooleanField(
        label=_('Atomic'),
        required=False,
        initial=True,
        help_text=_('Complete only if all changes from the source branch are applied successfully.')
    )
    # TODO: Populate choices for start & end fields dynamically
    start = forms.ModelChoiceField(
        queryset=ObjectChange.objects.none(),
        required=False
    )
    end = forms.ModelChoiceField(
        queryset=ObjectChange.objects.none(),
        required=False
    )

    field_order = ('source', 'atomic', 'start', 'end', 'commit')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['source'].queryset = Branch.objects.exclude(pk=self.branch.pk)

        if source_id := get_field_value(self, 'source'):
            try:
                source = Branch.objects.get(pk=source_id)
                unpulled_changes = self.branch.get_unpulled_changes(source)
                self.fields['start'].queryset = unpulled_changes
                self.fields['end'].queryset = unpulled_changes
            except Branch.DoesNotExist:
                pass


class ConfirmationForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label=_('Confirm')
    )
