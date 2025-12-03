from django.utils.translation import gettext_lazy as _, pgettext_lazy

from utilities.choices import ChoiceSet


class BranchStatusChoices(ChoiceSet):
    NEW = 'new'
    PROVISIONING = 'provisioning'
    READY = 'ready'
    SYNCING = 'syncing'
    MIGRATING = 'migrating'
    MERGING = 'merging'
    REVERTING = 'reverting'
    MERGED = 'merged'
    ARCHIVED = 'archived'
    PENDING_MIGRATIONS = 'pending-migrations'
    FAILED = 'failed'

    CHOICES = (
        (NEW, _('New'), 'cyan'),
        (PROVISIONING, _('Provisioning'), 'orange'),
        (READY, _('Ready'), 'green'),
        (SYNCING, _('Syncing'), 'orange'),
        (MIGRATING, _('Migrating'), 'orange'),
        (MERGING, _('Merging'), 'orange'),
        (REVERTING, _('Reverting'), 'orange'),
        (MERGED, _('Merged'), 'blue'),
        (ARCHIVED, _('Archived'), 'gray'),
        (PENDING_MIGRATIONS, _('Pending Migrations'), 'red'),
        (FAILED, _('Failed'), 'red'),
    )

    TRANSITIONAL = (
        PROVISIONING,
        SYNCING,
        MIGRATING,
        MERGING,
        REVERTING,
    )

    WORKING = (
        NEW,
        READY,
        PENDING_MIGRATIONS,
        *TRANSITIONAL,
    )


class BranchMergeStrategyChoices(ChoiceSet):
    ITERATIVE = 'iterative'
    SQUASH = 'squash'

    CHOICES = (
        (ITERATIVE, _('Iterative')),
        (SQUASH, pgettext_lazy('The act of compressing multiple records into one', 'Squash')),
    )


class BranchEventTypeChoices(ChoiceSet):
    PROVISIONED = 'provisioned'
    SYNCED = 'synced'
    MIGRATED = 'migrated'
    MERGED = 'merged'
    REVERTED = 'reverted'
    ARCHIVED = 'archived'

    CHOICES = (
        (PROVISIONED, _('Provisioned'), 'green'),
        (SYNCED, _('Synced'), 'cyan'),
        (MIGRATED, _('Migrated'), 'purple'),
        (MERGED, _('Merged'), 'blue'),
        (REVERTED, _('Reverted'), 'orange'),
        (ARCHIVED, _('Archived'), 'gray'),
    )
