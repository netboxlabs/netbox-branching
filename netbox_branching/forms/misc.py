from django import forms
from django.utils.translation import gettext_lazy as _

from netbox_branching.choices import BranchMergeStrategyChoices
from netbox_branching.models import Branch, ChangeDiff

__all__ = (
    'BranchMergeForm',
    'BranchRevertForm',
    'BranchSyncForm',
    'BulkMigrateBranchForm',
    'ConfirmationForm',
    'DescriptiveRadioSelect',
    'MigrateBranchForm',
)


class DescriptiveRadioSelect(forms.RadioSelect):
    """Radio select widget that renders a short description beneath each choice."""
    template_name = 'netbox_branching/widgets/radio_select.html'

    def __init__(self, *args, descriptions=None, **kwargs):
        self.descriptions = descriptions or {}
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        option['description'] = self.descriptions.get(str(value), '')
        return option


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
        help_text=_(
            'If unchecked, the operation is rolled back after completion and no changes are saved (dry run).'
        )
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
        selected_diffs = self.cleaned_data.get('pk', [])
        if conflicted_diffs and not set(conflicted_diffs).issubset(selected_diffs):
            raise forms.ValidationError(_("All conflicts must be acknowledged in order to merge the branch."))

        return self.cleaned_data


class BranchSyncForm(BaseBranchActionForm):
    """Form for syncing a branch."""


class BranchMergeForm(BaseBranchActionForm):
    """Form for merging a branch."""
    commit = forms.BooleanField(
        required=False,
        label=_('Commit changes'),
        help_text=_(
            '<ul class="mb-0 ps-3">'
            '<li>If checked, the merge is committed and the branch remains available for revert or archival.</li>'
            '<li>If unchecked, the operation is rolled back after completion and no changes are saved '
            '(dry run).</li>'
            '</ul>'
        )
    )
    merge_strategy = forms.ChoiceField(
        choices=BranchMergeStrategyChoices,
        initial=BranchMergeStrategyChoices.ITERATIVE,
        required=True,
        label=_('Merge Strategy'),
        widget=DescriptiveRadioSelect(descriptions={
            BranchMergeStrategyChoices.ITERATIVE: _(
                'Replay each change individually in order, preserving the full audit trail.'
            ),
            BranchMergeStrategyChoices.SQUASH: _(
                'Collapse all changes per object into a single create, update, or delete. Can resolve some '
                'merge cases that the iterative strategy cannot.'
            ),
        })
    )


class BranchRevertForm(BaseBranchActionForm):
    """Form for reverting a branch."""


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


class BulkMigrateBranchForm(forms.Form):
    pk = forms.ModelMultipleChoiceField(
        queryset=Branch.objects.all(),
        widget=forms.MultipleHiddenInput()
    )
