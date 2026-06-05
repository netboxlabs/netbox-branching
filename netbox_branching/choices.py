from typing import ClassVar

from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy
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

    DESCRIPTIONS: ClassVar = {
        NEW: _('Branch has been created but not yet provisioned.'),
        PROVISIONING: _('Branch database schema is being set up.'),
        READY: _('Branch is provisioned and ready for use.'),
        SYNCING: _('Branch is syncing changes from the main database.'),
        MIGRATING: _('Database migrations are being applied to the branch.'),
        MERGING: _('Branch changes are being merged into the main database.'),
        REVERTING: _('Branch merge is being reverted.'),
        MERGED: _('Branch has been successfully merged.'),
        ARCHIVED: _('Branch has been archived and is no longer active.'),
        PENDING_MIGRATIONS: _('Branch requires database migrations before it can be used.'),
        FAILED: _('A branch operation has failed.'),
    }

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
