import logging
import random
import string
from contextlib import nullcontext
from datetime import timedelta
from functools import cached_property, partial

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connection, models, transaction
from django.db.models.signals import post_save
from django.db.utils import ProgrammingError
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import ObjectChange as ObjectChange_
from netbox.config import get_config
from netbox.context import current_request
from netbox.context_managers import event_tracking
from netbox.models import PrimaryModel
from netbox.models.features import JobsMixin
from netbox.plugins import get_plugin_config
from netbox_branching.choices import BranchEventTypeChoices, BranchStatusChoices
from netbox_branching.constants import BRANCH_ACTIONS
from netbox_branching.contextvars import active_branch
from netbox_branching.signals import *
from netbox_branching.utilities import (
    BranchActionIndicator, ChangeSummary, activate_branch, get_branchable_object_types, get_tables_to_replicate,
    record_applied_change,
)
from utilities.exceptions import AbortRequest, AbortTransaction
from .changes import ObjectChange

__all__ = (
    'Branch',
    'BranchEvent',
)


class Branch(JobsMixin, PrimaryModel):
    name = models.CharField(
        verbose_name=_('name'),
        max_length=100,
        unique=True
    )
    owner = models.ForeignKey(
        to=get_user_model(),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='branches'
    )
    schema_id = models.CharField(
        max_length=8,
        unique=True,
        verbose_name=_('schema ID'),
        editable=False
    )
    status = models.CharField(
        verbose_name=_('status'),
        max_length=50,
        choices=BranchStatusChoices,
        default=BranchStatusChoices.NEW,
        editable=False
    )
    last_sync = models.DateTimeField(
        blank=True,
        null=True,
        editable=False
    )
    merged_time = models.DateTimeField(
        verbose_name=_('merged time'),
        blank=True,
        null=True
    )
    merged_by = models.ForeignKey(
        to=get_user_model(),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='+'
    )

    _preaction_validators = {
        'sync': set(),
        'pull': set(),
        'merge': set(),
        'revert': set(),
        'archive': set(),
    }

    class Meta:
        ordering = ('name',)
        verbose_name = _('branch')
        verbose_name_plural = _('branches')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Generate a random schema ID if this is a new Branch
        if self.pk is None:
            self.schema_id = self._generate_schema_id()

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_branching:branch', args=[self.pk])

    def get_status_color(self):
        return BranchStatusChoices.colors.get(self.status)

    def clone(self):
        """
        Override CloningMixin's clone() method to nullify active branch and populate clone_from field.
        """
        return {
            '_branch': '',
            'clone_from': self.pk,
            **super().clone(),
        }

    @cached_property
    def is_active(self):
        return self == active_branch.get()

    @property
    def ready(self):
        return self.status == BranchStatusChoices.READY

    @property
    def merged(self):
        return self.status == BranchStatusChoices.MERGED

    @cached_property
    def schema_name(self):
        schema_prefix = get_plugin_config('netbox_branching', 'schema_prefix')
        return f'{schema_prefix}{self.schema_id}'

    @cached_property
    def connection_name(self):
        return f'schema_{self.schema_name}'

    @property
    def synced_time(self):
        return self.last_sync or self.created

    def clean(self):

        # Enforce the maximum number of total branches
        if not self.pk and (max_branches := get_plugin_config('netbox_branching', 'max_branches')):
            total_branch_count = Branch.objects.exclude(status=BranchStatusChoices.ARCHIVED).count()
            if total_branch_count >= max_branches:
                raise ValidationError(
                    _(
                        "The configured maximum number of non-archived branches ({max}) cannot be exceeded. One or "
                        "more existing branches must be deleted before a new branch may be created."
                    ).format(max=max_branches)
                )

        # Enforce the maximum number of active branches
        if not self.pk and (max_working_branches := get_plugin_config('netbox_branching', 'max_working_branches')):
            working_branch_count = Branch.objects.filter(status__in=BranchStatusChoices.WORKING).count()
            if working_branch_count >= max_working_branches:
                raise ValidationError(
                    _(
                        "The configured maximum number of working branches ({max}) cannot be exceeded. One or more "
                        "working branches must be merged or archived before a new branch may be created."
                    ).format(max=max_working_branches)
                )

    def save(self, provision=True, *args, **kwargs):
        """
        Args:
            provision: If True, automatically enqueue a background Job to provision the Branch. (Set this
                       to False if you will call provision() on the instance manually.)
        """
        from netbox_branching.jobs import ProvisionBranchJob, PullBranchJob

        _provision = provision and self.pk is None

        super().save(*args, **kwargs)

        if _provision:
            # Enqueue a background job to provision the Branch
            request = current_request.get()
            ProvisionBranchJob.enqueue(
                instance=self,
                user=request.user if request else None
            )

            # If cloning from an existing Branch, also enqueue a PullBranchJob
            if clone_source := getattr(self, '_clone_source', None):
                PullBranchJob.enqueue(
                    instance=self,
                    user=request.user,
                    source=clone_source,
                    atomic=getattr(self, '_clone_atomic', True),
                    commit=True
                )

    def delete(self, *args, **kwargs):
        if active_branch.get() == self:
            raise AbortRequest(_("The active branch cannot be deleted."))

        # Deprovision the schema
        self.deprovision()

        return super().delete(*args, **kwargs)

    @staticmethod
    def _generate_schema_id(length=8):
        """
        Generate a random alphanumeric schema identifier of the specified length.
        """
        chars = [*string.ascii_lowercase, *string.digits]
        return ''.join(random.choices(chars, k=length))

    @classmethod
    def register_preaction_check(cls, func, action):
        """
        Register a validator to run before a specific branch action (i.e. sync or merge).
        """
        if action not in BRANCH_ACTIONS:
            raise ValueError(f"Invalid branch action: {action}")
        cls._preaction_validators[action].add(func)

    def get_changes(self):
        """
        Return a queryset of all ObjectChange records created within the Branch.
        """
        if self.status == BranchStatusChoices.NEW:
            return ObjectChange.objects.none()
        return ObjectChange.objects.using(self.connection_name)

    def get_unsynced_changes(self):
        """
        Return a queryset of all ObjectChange records created in main since the Branch was last synced or created.
        """
        if self.status not in BranchStatusChoices.WORKING:
            return ObjectChange.objects.none()
        return ObjectChange.objects.using(DEFAULT_DB_ALIAS).exclude(
            application__branch=self
        ).filter(
            changed_object_type__in=get_branchable_object_types(),
            time__gt=self.synced_time
        )

    def get_unmerged_changes(self):
        """
        Return a queryset of all unmerged ObjectChange records within the Branch schema.
        """
        if self.status not in BranchStatusChoices.WORKING:
            return ObjectChange.objects.none()
        return ObjectChange.objects.using(self.connection_name)

    def get_merged_changes(self):
        """
        Return a queryset of all merged ObjectChange records for the Branch.
        """
        if self.status not in (BranchStatusChoices.MERGED, BranchStatusChoices.ARCHIVED):
            return ObjectChange.objects.none()
        return ObjectChange.objects.using(DEFAULT_DB_ALIAS).filter(
            application__branch=self
        )

    def get_unpulled_changes(self, source, start=None, end=None):
        """
        Return a queryset of all ObjectChange records from the source Branch which have yet to be replayed onto
        this Branch.
        """
        if source.status not in BranchStatusChoices.WORKING:
            return ObjectChange.objects.none()

        changes = ObjectChange.objects.using(source.connection_name).order_by('time')

        # Filter by starting change (if specified), or the time of the most recent pull event.
        if start:
            changes = changes.filter(pk__gte=start.pk)
        elif last_pull := self.events.filter(related_branch=source, type=BranchEventTypeChoices.PULLED).first():
            changes = changes.filter(time__gt=last_pull.time)

        # Filter by end change (if specified)
        if end:
            changes = changes.filter(pk__lte=end.pk)

        return changes

    def get_event_history(self):
        history = []
        last_time = timezone.now()
        for event in self.events.all():
            if change_count := self.get_changes().filter(time__gte=event.time, time__lt=last_time).count():
                summary = ChangeSummary(
                    start=event.time,
                    end=last_time,
                    count=change_count
                )
                history.append(summary)
            history.append(event)
            last_time = event.time
        return history

    @property
    def is_stale(self):
        """
        Indicates whether the branch is too far out of date to be synced.
        """
        if not (changelog_retention := get_config().CHANGELOG_RETENTION):
            # Changelog retention is disabled
            return False
        return self.synced_time < timezone.now() - timedelta(days=changelog_retention)

    #
    # Branch action indicators
    #

    def _can_do_action(self, action):
        """
        Execute any validators configured for the specified branch
        action. Return False if any fail; otherwise return True.
        """
        if action not in BRANCH_ACTIONS:
            raise Exception(f"Unrecognized branch action: {action}")

        # Run any pre-action validators
        for func in self._preaction_validators[action]:
            if not (indicator := func(self)):
                # Backward compatibility for pre-v0.6.0 validators
                if type(indicator) is not BranchActionIndicator:
                    return BranchActionIndicator(False, _(f"Validation failed for {action}: {func}"))
                return indicator

        return BranchActionIndicator(True)

    @cached_property
    def can_sync(self):
        """
        Indicates whether the branch can be synced.
        """
        return self._can_do_action('sync')

    @cached_property
    def can_pull(self):
        """
        Indicates whether changes can be pulled in from another Branch.
        """
        return self._can_do_action('pull')

    @cached_property
    def can_merge(self):
        """
        Indicates whether the branch can be merged.
        """
        return self._can_do_action('merge')

    @cached_property
    def can_revert(self):
        """
        Indicates whether the branch can be reverted.
        """
        return self._can_do_action('revert')

    @cached_property
    def can_archive(self):
        """
        Indicates whether the branch can be archived.
        """
        return self._can_do_action('archive')

    #
    # Branch actions
    #

    def sync(self, user, commit=True):
        """
        Apply changes from the main schema onto the Branch's schema.
        """
        logger = logging.getLogger('netbox_branching.branch.sync')
        logger.info(f'Syncing branch {self} ({self.schema_name})')

        if not self.ready:
            raise Exception(f"Branch {self} is not ready to sync")
        if self.is_stale:
            raise Exception(f"Branch {self} is stale and can no longer be synced")
        if commit and not self.can_sync:
            raise Exception(f"Syncing this branch is not permitted.")

        # Emit pre-sync signal
        pre_sync.send(sender=self.__class__, branch=self, user=user)

        # Retrieve unsynced changes before we update the Branch's status
        if changes := self.get_unsynced_changes().order_by('time'):
            logger.info(f"Found {len(changes)} changes to sync")
        else:
            logger.info(f"No changes found; aborting.")
            return

        # Update Branch status
        logger.debug(f"Setting branch status to {BranchStatusChoices.SYNCING}")
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.SYNCING)

        try:
            with activate_branch(self):
                with transaction.atomic(using=self.connection_name):
                    # Apply each change from the main schema
                    for change in changes:
                        change.apply(using=self.connection_name, logger=logger)
                    if not commit:
                        raise AbortTransaction()

        except Exception as e:
            if err_message := str(e):
                logger.error(err_message)
            # Restore original branch status
            Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.READY)
            raise e

        # Record the branch's last_synced time & update its status
        logger.debug(f"Setting branch status to {BranchStatusChoices.READY}")
        self.last_sync = timezone.now()
        self.status = BranchStatusChoices.READY
        self.save()

        # Record a branch event for the sync
        logger.debug(f"Recording branch event: {BranchEventTypeChoices.SYNCED}")
        BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.SYNCED)

        # Emit post-sync signal
        post_sync.send(sender=self.__class__, branch=self, user=user)

        logger.info('Syncing completed')

    sync.alters_data = True

    def pull(self, source, user, atomic=True, start=None, end=None, commit=True):
        """
        Replicate all unpulled changes from the source branch into this one.
        """
        logger = logging.getLogger('netbox_branching.branch.pull')
        logger.info(f'Pulling changes from branch {source} into {self.name}')

        if not self.ready:
            raise Exception(f"Branch {self} is not ready for changes.")
        if not source.ready:
            raise Exception(f"Changes cannot be pulled from branch {source} at this time.")
        if commit and not self.can_pull:
            raise Exception(f"Pulling changes to this branch is not permitted.")

        # Emit pre-pull signal
        pre_pull.send(sender=self.__class__, branch=self, user=user)

        # Retrieve staged changes before we update the Branch's status
        if changes := self.get_unpulled_changes(source, start=start, end=end):
            logger.info(f"Found {len(changes)} changes to pull")
        else:
            logger.info(f"No changes found; aborting.")
            return

        # Create a dummy request for the event_tracking() context manager
        request = RequestFactory().get(reverse('home'))

        try:
            use_atomic = atomic or not commit
            with transaction.atomic(using=self.connection_name) if use_atomic else nullcontext():
                # Apply each change from the Branch
                for change in changes:
                    with event_tracking(request):
                        request.id = change.request_id
                        request.user = change.user
                        change.apply(using=self.connection_name, logger=logger)
                if not commit:
                    raise AbortTransaction()

        except Exception as e:
            if err_message := str(e):
                logger.error(err_message)
            if atomic:
                raise e

        # Record a branch event for the merge
        logger.debug(f"Recording branch event: {BranchEventTypeChoices.PULLED}")
        BranchEvent.objects.create(branch=self, related_branch=source, user=user, type=BranchEventTypeChoices.PULLED)

        # Emit post-pull signal
        post_pull.send(sender=self.__class__, branch=self, user=user)

        logger.info('Pull completed')

    pull.alters_data = True

    def merge(self, user, commit=True):
        """
        Apply all changes in the Branch to the main schema by replaying them in
        chronological order.
        """
        logger = logging.getLogger('netbox_branching.branch.merge')
        logger.info(f'Merging branch {self} ({self.schema_name})')

        if not self.ready:
            raise Exception(f"Branch {self} is not ready to merge")
        if commit and not self.can_merge:
            raise Exception(f"Merging this branch is not permitted.")

        # Emit pre-merge signal
        pre_merge.send(sender=self.__class__, branch=self, user=user)

        # Retrieve staged changes before we update the Branch's status
        if changes := self.get_unmerged_changes().order_by('time'):
            logger.info(f"Found {len(changes)} changes to merge")
        else:
            logger.info(f"No changes found; aborting.")
            return

        # Update Branch status
        logger.debug(f"Setting branch status to {BranchStatusChoices.MERGING}")
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.MERGING)

        # Create a dummy request for the event_tracking() context manager
        request = RequestFactory().get(reverse('home'))

        # Prep & connect the signal receiver for recording AppliedChanges
        handler = partial(record_applied_change, branch=self)
        post_save.connect(handler, sender=ObjectChange_, weak=False)

        try:
            with transaction.atomic():
                # Apply each change from the Branch
                for change in changes:
                    with event_tracking(request):
                        request.id = change.request_id
                        request.user = change.user
                        change.apply(using=DEFAULT_DB_ALIAS, logger=logger)
                if not commit:
                    raise AbortTransaction()

        except Exception as e:
            if err_message := str(e):
                logger.error(err_message)
            # Disconnect signal receiver & restore original branch status
            post_save.disconnect(handler, sender=ObjectChange_)
            Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.READY)
            raise e

        # Update the Branch's status to "merged"
        logger.debug(f"Setting branch status to {BranchStatusChoices.MERGED}")
        self.status = BranchStatusChoices.MERGED
        self.merged_time = timezone.now()
        self.merged_by = user
        self.save()

        # Record a branch event for the merge
        logger.debug(f"Recording branch event: {BranchEventTypeChoices.MERGED}")
        BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.MERGED)

        # Emit post-merge signal
        post_merge.send(sender=self.__class__, branch=self, user=user)

        logger.info('Merging completed')

        # Disconnect the signal receiver
        post_save.disconnect(handler, sender=ObjectChange_)

    merge.alters_data = True

    def revert(self, user, commit=True):
        """
        Undo all changes associated with a previously merged Branch in the main schema by replaying them in
        reverse order and calling undo() on each.
        """
        logger = logging.getLogger('netbox_branching.branch.revert')
        logger.info(f'Reverting branch {self} ({self.schema_name})')

        if not self.merged:
            raise Exception(f"Only merged branches can be reverted.")
        if commit and not self.can_revert:
            raise Exception(f"Reverting this branch is not permitted.")

        # Emit pre-revert signal
        pre_revert.send(sender=self.__class__, branch=self, user=user)

        # Retrieve applied changes before we update the Branch's status
        if changes := self.get_changes().order_by('-time'):
            logger.info(f"Found {len(changes)} changes to revert")
        else:
            logger.info(f"No changes found; aborting.")
            return

        # Update Branch status
        logger.debug(f"Setting branch status to {BranchStatusChoices.REVERTING}")
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.REVERTING)

        # Create a dummy request for the event_tracking() context manager
        request = RequestFactory().get(reverse('home'))

        # Prep & connect the signal receiver for recording AppliedChanges
        handler = partial(record_applied_change, branch=self)
        post_save.connect(handler, sender=ObjectChange_, weak=False)

        try:
            with transaction.atomic():
                # Undo each change from the Branch
                for change in changes:
                    with event_tracking(request):
                        request.id = change.request_id
                        request.user = change.user
                        change.undo(logger=logger)
                if not commit:
                    raise AbortTransaction()

        except Exception as e:
            if err_message := str(e):
                logger.error(err_message)
            # Disconnect signal receiver & restore original branch status
            post_save.disconnect(handler, sender=ObjectChange_)
            Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.MERGED)
            raise e

        # Update the Branch's status to "ready"
        logger.debug(f"Setting branch status to {BranchStatusChoices.READY}")
        self.status = BranchStatusChoices.READY
        self.merged_time = None
        self.merged_by = None
        self.save()

        # Record a branch event for the merge
        logger.debug(f"Recording branch event: {BranchEventTypeChoices.REVERTED}")
        BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.REVERTED)

        # Emit post-revert signal
        post_revert.send(sender=self.__class__, branch=self, user=user)

        logger.info('Reversion completed')

        # Disconnect the signal receiver
        post_save.disconnect(handler, sender=ObjectChange_)

    revert.alters_data = True

    def provision(self, user):
        """
        Create the schema & replicate main tables.
        """
        logger = logging.getLogger('netbox_branching.branch.provision')
        logger.info(f'Provisioning branch {self} ({self.schema_name})')

        # Emit pre-provision signal
        pre_provision.send(sender=self.__class__, branch=self, user=user)

        # Update Branch status
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.PROVISIONING)

        with connection.cursor() as cursor:
            try:
                schema = self.schema_name

                # Start a transaction
                cursor.execute("BEGIN")
                cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")

                # Create the new schema
                logger.debug(f'Creating schema {schema}')
                try:
                    cursor.execute(f"CREATE SCHEMA {schema}")
                except ProgrammingError as e:
                    if str(e).startswith('permission denied '):
                        logger.critical(
                            f"Provisioning failed due to insufficient database permissions. Ensure that the NetBox "
                            f"role ({settings.DATABASE['USER']}) has permission to create new schemas on this "
                            f"database ({settings.DATABASE['NAME']}). (Use the PostgreSQL command 'GRANT CREATE ON "
                            f"DATABASE $database TO $role;' to grant the required permission.)"
                        )
                    raise e

                # Create an empty copy of the global change log. Share the ID sequence from the main table to avoid
                # reusing change record IDs.
                table = ObjectChange_._meta.db_table
                main_table = f'public.{table}'
                schema_table = f'{schema}.{table}'
                logger.debug(f'Creating table {schema_table}')
                cursor.execute(
                    f"CREATE TABLE {schema_table} ( LIKE {main_table} INCLUDING INDEXES )"
                )
                # Set the default value for the ID column to the sequence associated with the source table
                sequence_name = f'public.{table}_id_seq'
                cursor.execute(
                    f"ALTER TABLE {schema_table} ALTER COLUMN id SET DEFAULT nextval(%s)", [sequence_name]
                )

                # Replicate relevant tables from the main schema
                for table in get_tables_to_replicate():
                    main_table = f'public.{table}'
                    schema_table = f'{schema}.{table}'
                    logger.debug(f'Creating table {schema_table}')
                    # Create the table in the new schema
                    cursor.execute(
                        f"CREATE TABLE {schema_table} ( LIKE {main_table} INCLUDING INDEXES )"
                    )
                    # Copy data from the source table
                    cursor.execute(
                        f"INSERT INTO {schema_table} SELECT * FROM {main_table}"
                    )
                    # Get the name of the sequence used for object ID allocations
                    cursor.execute(
                        "SELECT pg_get_serial_sequence(%s, 'id')", [table]
                    )
                    sequence_name = cursor.fetchone()[0]
                    # Set the default value for the ID column to the sequence associated with the source table
                    cursor.execute(
                        f"ALTER TABLE {schema_table} ALTER COLUMN id SET DEFAULT nextval(%s)", [sequence_name]
                    )

                # Commit the transaction
                cursor.execute("COMMIT")

            except Exception as e:
                # Abort the transaction
                cursor.execute("ROLLBACK")

                # Mark the Branch as failed
                logger.error(e)
                Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.FAILED)

                raise e

        # Emit post-provision signal
        post_provision.send(sender=self.__class__, branch=self, user=user)

        logger.info('Provisioning completed')

        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.READY)
        BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.PROVISIONED)

    provision.alters_data = True

    def archive(self, user):
        """
        Deprovision the Branch and set its status to "archived."
        """
        if not self.can_archive:
            raise Exception(f"Archiving this branch is not permitted.")

        # Deprovision the branch's schema
        self.deprovision()

        # Update the branch's status to "archived"
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.ARCHIVED)
        BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.ARCHIVED)

    archive.alters_data = True

    def deprovision(self):
        """
        Delete the Branch's schema and all its tables from the database.
        """
        logger = logging.getLogger('netbox_branching.branch.provision')
        logger.info(f'Deprovisioning branch {self} ({self.schema_name})')

        # Emit pre-deprovision signal
        pre_deprovision.send(sender=self.__class__, branch=self)

        with connection.cursor() as cursor:
            # Delete the schema and all its tables
            logger.debug(f'Deleting schema {self.schema_name}')
            cursor.execute(
                f"DROP SCHEMA IF EXISTS {self.schema_name} CASCADE"
            )

        # Emit post-deprovision signal
        post_deprovision.send(sender=self.__class__, branch=self)

        logger.info('Deprovisioning completed')

    deprovision.alters_data = True


class BranchEvent(models.Model):
    time = models.DateTimeField(
        auto_now_add=True,
        editable=False
    )
    branch = models.ForeignKey(
        to='netbox_branching.branch',
        on_delete=models.CASCADE,
        related_name='events'
    )
    related_branch = models.ForeignKey(
        to='netbox_branching.branch',
        on_delete=models.CASCADE,
        related_name='+',
        blank=True,
        null=True
    )
    user = models.ForeignKey(
        to=get_user_model(),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='branch_events'
    )
    type = models.CharField(
        verbose_name=_('type'),
        max_length=50,
        choices=BranchEventTypeChoices,
        editable=False
    )

    class Meta:
        ordering = ('-time',)
        verbose_name = _('branch event')
        verbose_name_plural = _('branch events')

    def get_type_color(self):
        return BranchEventTypeChoices.colors.get(self.type)
