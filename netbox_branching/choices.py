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

    # The status to which a branch is reset when the background job responsible for a
    # transitional status is no longer running (e.g. its worker was killed). Each value
    # mirrors the status that the corresponding branch operation restores itself when it
    # fails, so recovery leaves the branch exactly where a caught error would have. See #622.
    RECOVERY_STATUS: ClassVar = {
        # A partially provisioned schema cannot be resumed; the branch must be deleted or
        # re-created, which is what the provisioning failure path also does.
        PROVISIONING: FAILED,
        SYNCING: READY,
        # Django applies each migration in its own transaction, so an interrupted migration
        # leaves the branch consistent but partially migrated: the remaining migrations can
        # simply be re-applied.
        MIGRATING: PENDING_MIGRATIONS,
        # Merges and reverts run inside a single transaction, which the database rolls back
        # when the connection dies, so the branch is left as it was before the operation.
        MERGING: READY,
        REVERTING: MERGED,
    }

    # What recovery does for each transitional status, and the state the interrupted operation
    # leaves the branch's data in. Shown on the recovery confirmation form so that the operator
    # can see what is and is not lost before resetting the status. See #622.
    RECOVERY_DESCRIPTIONS: ClassVar = {
        PROVISIONING: _(
            'The partial schema cannot be resumed; the branch will be marked as failed. Delete it and '
            'create a new one.'
        ),
        SYNCING: _('The interrupted sync was rolled back; the branch can be synced again.'),
        MIGRATING: _('The outstanding migrations can be applied after recovery.'),
        MERGING: _('The interrupted merge was rolled back; nothing reached main and it can be merged again.'),
        REVERTING: _('The interrupted revert was rolled back; the changes are still in main and can be reverted.'),
    }

    # Transitional statuses whose interrupted operation can simply be re-run once the branch has
    # been reset, sparing the operator from re-initiating it by hand. Both are confined to the
    # branch's own schema and take no parameters which recovery cannot reconstruct.
    #
    # Merging and reverting are deliberately excluded: they write to the main schema, and the
    # `commit` flag which distinguishes a dry run from a real run is handed to RQ rather than
    # stored on the Job, so it dies with the worker. Retrying one would risk committing an
    # operation which was only ever meant to be a dry run. Provisioning is excluded because a
    # partially created schema cannot be resumed at all. See #622.
    RECOVERY_RETRYABLE: ClassVar = (
        SYNCING,
        MIGRATING,
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
