import hashlib

from django import forms
from django.db.models import Count, Max
from django.utils.translation import gettext_lazy as _

from netbox_branching.choices import BranchMergeStrategyChoices, BranchStatusChoices
from netbox_branching.models import Branch, ChangeDiff

__all__ = (
    'BranchMergeForm',
    'BranchRevertForm',
    'BranchSyncForm',
    'BulkMigrateBranchForm',
    'ConfirmationForm',
    'DescriptiveRadioSelect',
    'MigrateBranchForm',
    'RecoverBranchForm',
)


def get_conflicts_token(branch):
    """
    Return a fingerprint of the branch's conflicting ChangeDiffs, or an empty string if there are none.
    """
    stats = ChangeDiff.objects.filter(branch=branch, conflicts__isnull=False).aggregate(
        count=Count('pk'), max_pk=Max('pk'), last_updated=Max('last_updated')
    )
    if not stats['count']:
        return ''
    fingerprint = f'{stats["count"]}:{stats["max_pk"]}:{stats["last_updated"].isoformat()}'
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


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

    acknowledge_conflicts = forms.BooleanField(
        required=False,
        label=_('Acknowledge conflicts'),
        help_text=_('Confirm that the conflicting changes listed above may overwrite data in the target.'),
    )
    conflicts_token = forms.CharField(required=False, widget=forms.HiddenInput())
    commit = forms.BooleanField(
        required=False,
        label=_('Commit changes'),
        help_text=_('If unchecked, the operation is rolled back after completion and no changes are saved (dry run).'),
    )

    def __init__(self, branch, *args, allow_commit=True, **kwargs):
        self.branch = branch
        super().__init__(*args, **kwargs)

        if not allow_commit:
            self.fields['commit'].disabled = True

        # The token identifies the set of conflicts shown to the user, so acknowledging them doesn't
        # require submitting every conflicting ChangeDiff
        self.current_conflicts_token = get_conflicts_token(branch)
        if self.current_conflicts_token:
            self.fields['conflicts_token'].initial = self.current_conflicts_token
        else:
            del self.fields['acknowledge_conflicts']
            del self.fields['conflicts_token']

    def clean(self):
        super().clean()

        if not self.current_conflicts_token:
            return self.cleaned_data

        if self.cleaned_data.get('conflicts_token') != self.current_conflicts_token:
            # Re-render with the current token so the user can acknowledge the updated conflicts
            self.data = self.data.copy()
            self.data['conflicts_token'] = self.current_conflicts_token
            raise forms.ValidationError(
                _('The conflicts for this branch have changed. Please review them again before proceeding.')
            )
        if not self.cleaned_data.get('acknowledge_conflicts'):
            raise forms.ValidationError(_('All conflicts must be acknowledged in order to proceed.'))

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
        ),
    )
    merge_strategy = forms.ChoiceField(
        choices=BranchMergeStrategyChoices,
        initial=BranchMergeStrategyChoices.ITERATIVE,
        required=True,
        label=_('Merge Strategy'),
        widget=DescriptiveRadioSelect(
            descriptions={
                BranchMergeStrategyChoices.ITERATIVE: _(
                    'Replay each change individually in order, preserving the full audit trail.'
                ),
                BranchMergeStrategyChoices.SQUASH: _(
                    'Collapse all changes per object into a single create, update, or delete. Can resolve some '
                    'merge cases that the iterative strategy cannot.'
                ),
            }
        ),
    )


class BranchRevertForm(BaseBranchActionForm):
    """Form for reverting a branch."""


class ConfirmationForm(forms.Form):
    confirm = forms.BooleanField(required=True, label=_('Confirm'))


class MigrateBranchForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label=_('Confirm migrations'),
        help_text=_(
            'All migrations will be applied in order. <strong>Migrations cannot be reversed once applied.</strong>'
        ),
    )


# How the retry option is described for each operation which can be re-run on recovery.
RECOVERY_RETRY_LABELS = {
    BranchStatusChoices.SYNCING: _('Sync the branch after resetting'),
    BranchStatusChoices.MIGRATING: _('Apply the outstanding migrations after resetting'),
}


class RecoverBranchForm(forms.Form):
    """
    Options for resetting a branch stuck in a transitional status (#622). Submitting the form is
    itself the confirmation, so the only field offered is whether to pick the interrupted operation
    back up once the status has been reset — and that is offered only for the operations which can
    be re-run safely (see BranchStatusChoices.RECOVERY_RETRYABLE). For every other status the form
    has no fields at all.

    Re-running is off by default: the worker may well have died because of the operation itself, in
    which case running it again unprompted would simply repeat the failure.
    """

    retry = forms.BooleanField(required=False, initial=False)

    def __init__(self, branch, *args, **kwargs):
        self.branch = branch
        super().__init__(*args, **kwargs)

        if branch.status in BranchStatusChoices.RECOVERY_RETRYABLE:
            self.fields['retry'].label = RECOVERY_RETRY_LABELS[branch.status]
        else:
            del self.fields['retry']


class BulkMigrateBranchForm(forms.Form):
    pk = forms.ModelMultipleChoiceField(queryset=Branch.objects.all(), widget=forms.MultipleHiddenInput())
