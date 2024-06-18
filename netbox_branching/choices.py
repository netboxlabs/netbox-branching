from django.utils.translation import gettext_lazy as _

from utilities.choices import ChoiceSet


class BranchStatusChoices(ChoiceSet):
    NEW = 'new'
    PROVISIONING = 'provisioning'
    READY = 'ready'
    SYNCING = 'syncing'
    MERGING = 'merging'
    MERGED = 'merged'
    FAILED = 'failed'

    CHOICES = (
        (NEW, _('New'), 'cyan'),
        (PROVISIONING, _('Provisioning'), 'orange'),
        (READY, _('Ready'), 'green'),
        (SYNCING, _('Syncing'), 'orange'),
        (MERGING, _('Merging'), 'orange'),
        (MERGED, _('Merged'), 'blue'),
        (FAILED, _('Failed'), 'red'),
    )
