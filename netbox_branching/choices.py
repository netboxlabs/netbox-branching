from django.utils.translation import gettext_lazy as _

from utilities.choices import ChoiceSet


class BranchStatusChoices(ChoiceSet):
    NEW = 'new'
    PROVISIONING = 'provisioning'
    READY = 'ready'
    SYNCING = 'syncing'
    MERGING = 'merging'
    REVERTING = 'reverting'
    MERGED = 'merged'
    FAILED = 'failed'

    CHOICES = (
        (NEW, _('New'), 'cyan'),
        (PROVISIONING, _('Provisioning'), 'orange'),
        (READY, _('Ready'), 'green'),
        (SYNCING, _('Syncing'), 'orange'),
        (MERGING, _('Merging'), 'orange'),
        (REVERTING, _('Reverting'), 'orange'),
        (MERGED, _('Merged'), 'blue'),
        (FAILED, _('Failed'), 'red'),
    )

    TRANSITIONAL = (
        PROVISIONING,
        SYNCING,
        MERGING,
        REVERTING
    )


class BranchEventTypeChoices(ChoiceSet):
    PROVISIONED = 'provisioned'
    SYNCED = 'synced'
    MERGED = 'merged'
    REVERTED = 'reverted'

    CHOICES = (
        (PROVISIONED, _('Provisioned'), 'green'),
        (SYNCED, _('Synced'), 'cyan'),
        (MERGED, _('Merged'), 'blue'),
        (REVERTED, _('Reverted'), 'orange'),
    )
