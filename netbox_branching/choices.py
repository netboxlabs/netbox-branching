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
    ARCHIVED = 'archived'
    FAILED = 'failed'

    CHOICES = (
        (NEW, _('New'), 'cyan'),
        (PROVISIONING, _('Provisioning'), 'orange'),
        (READY, _('Ready'), 'green'),
        (SYNCING, _('Syncing'), 'orange'),
        (MERGING, _('Merging'), 'orange'),
        (REVERTING, _('Reverting'), 'orange'),
        (MERGED, _('Merged'), 'blue'),
        (ARCHIVED, _('Archived'), 'gray'),
        (FAILED, _('Failed'), 'red'),
    )

    TRANSITIONAL = (
        PROVISIONING,
        SYNCING,
        MERGING,
        REVERTING,
    )

    WORKING = (
        NEW,
        READY,
        *TRANSITIONAL,
    )


class BranchEventTypeChoices(ChoiceSet):
    PROVISIONED = 'provisioned'
    SYNCED = 'synced'
    MERGED = 'merged'
    REVERTED = 'reverted'
    ARCHIVED = 'archived'

    CHOICES = (
        (PROVISIONED, _('Provisioned'), 'green'),
        (SYNCED, _('Synced'), 'cyan'),
        (MERGED, _('Merged'), 'blue'),
        (REVERTED, _('Reverted'), 'orange'),
        (ARCHIVED, _('Archived'), 'gray'),
    )
