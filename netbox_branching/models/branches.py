import importlib
import logging
import math
import uuid
from collections import defaultdict
from datetime import timedelta
from functools import cached_property, partial

from core.choices import JobStatusChoices, ObjectChangeActionChoices
from core.models import ObjectChange as ObjectChange_
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, models, transaction
from django.db.models.signals import post_save, pre_delete
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from netbox.config import get_config
from netbox.context import current_request
from netbox.models import PrimaryModel
from netbox.models.features import JobsMixin
from netbox.plugins import get_plugin_config
from utilities.exceptions import AbortRequest, AbortTransaction
from utilities.querysets import RestrictedQuerySet
from utilities.serialization import serialize_object

from netbox_branching.backends import get_branching_backend
from netbox_branching.choices import BranchEventTypeChoices, BranchMergeStrategyChoices, BranchStatusChoices
from netbox_branching.constants import BRANCH_ACTIONS
from netbox_branching.contextvars import active_branch
from netbox_branching.merge_strategies import get_merge_strategy
from netbox_branching.signals import *
from netbox_branching.utilities import (
    BranchActionIndicator,
    ChangeSummary,
    activate_branch,
    get_branchable_object_types,
    is_job_abandoned,
    record_applied_change,
)

from .changes import ChangeDiff, ObjectChange

__all__ = (
    'Branch',
    'BranchEvent',
)


def _serialize_for_sync(obj):
    """
    Serialize an object for sync-time pre/post snapshots, matching the format
    record_change_diff expects on ObjectChange.postchange_data_clean.

    Defensive: every branchable model inherits ChangeLoggingMixin.serialize_object,
    so the utility-function fallback should not be reached in practice.
    """
    if hasattr(obj, 'serialize_object'):
        return obj.serialize_object(exclude=['created', 'last_updated'])
    return serialize_object(obj, exclude=['created', 'last_updated'])


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
    backend_id = models.CharField(
        max_length=255,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_('backend ID'),
        editable=False,
        help_text=_('Unique identifier assigned by the branching backend during provisioning')
    )
    status = models.CharField(
        verbose_name=_('status'),
        max_length=50,
        choices=BranchStatusChoices,
        default=BranchStatusChoices.NEW,
        editable=False
    )
    applied_migrations = ArrayField(
        verbose_name=_('applied migrations'),
        base_field=models.CharField(max_length=200),
        blank=True,
        default=list,
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
    merge_strategy = models.CharField(
        verbose_name=_('merge strategy'),
        max_length=50,
        choices=BranchMergeStrategyChoices,
        blank=True,
        null=True,
        default=None,
        help_text=_('Strategy used to merge this branch')
    )
    connection_params = models.JSONField(
        verbose_name=_('connection parameters'),
        blank=True,
        null=True,
        editable=False,
        help_text=_('Backend-specific parameters for connecting to this branch')
    )

    _preaction_validators = {
        'sync': set(),
        'migrate': set(),
        'merge': set(),
        'revert': set(),
        'archive': set(),
    }

    class Meta:
        ordering = ('name',)
        permissions = [
            ('sync', 'Synchronize branch with main schema'),
            ('merge', 'Merge branch changes into main'),
            ('migrate', 'Apply pending migrations to branch'),
            ('revert', 'Revert a merged branch'),
            ('archive', 'Archive a merged branch'),
        ]
        verbose_name = _('branch')
        verbose_name_plural = _('branches')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_branching:branch', args=[self.pk])

    def get_status_color(self):
        return BranchStatusChoices.colors.get(self.status)

    def get_status_description(self):
        return BranchStatusChoices.DESCRIPTIONS.get(self.status, '')

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
    def backend(self):
        """
        The configured branching backend, which owns the mechanism by which this branch's
        dataset is isolated from main. See netbox_branching.backends.BranchingBackend.
        """
        return get_branching_backend()

    @cached_property
    def schema_name(self):
        if not self.backend_id:
            raise ValueError(
                f"Branch {self} has no backend ID; it has not yet been provisioned."
            )
        schema_prefix = get_plugin_config('netbox_branching', 'schema_prefix')
        return f'{schema_prefix}{self.backend_id}'

    @cached_property
    def connection_name(self):
        return self.backend.get_connection_alias(self)

    def clean(self):
        super().clean()

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

    # Fields owned by background jobs and by the branching backend; excluded from save() by default to
    # avoid clobbering.
    LIFECYCLE_FIELDS = (
        'status', 'last_sync', 'merged_time', 'merged_by', 'applied_migrations', 'backend_id',
        'connection_params',
    )

    def save(self, provision=True, update_merge_sync_fields=False, *args, **kwargs):
        """
        Args:
            provision: If True, automatically enqueue a background Job to provision the Branch. (Set this
                       to False if you will call provision() on the instance manually.)
            update_merge_sync_fields: If True, the caller intends to write lifecycle fields and the
                       pre-save exclusion is skipped. Used by internal lifecycle methods
                       (sync/migrate/merge/revert).
        """
        from netbox_branching.jobs import ProvisionBranchJob

        _provision = provision and self.pk is None
        _shield_lifecycle = (
            self.pk and not update_merge_sync_fields and 'update_fields' not in kwargs
        )

        if _shield_lifecycle:
            kwargs['update_fields'] = [
                f.name for f in self._meta.concrete_fields
                if f.name not in self.LIFECYCLE_FIELDS and not f.primary_key
            ]

        super().save(*args, **kwargs)

        if _shield_lifecycle:
            # Refresh excluded fields so callers (notably DRF response serializers) see current state.
            self.refresh_from_db(fields=self.LIFECYCLE_FIELDS)

        if _provision:
            # Enqueue a background job to provision the Branch
            request = current_request.get()
            ProvisionBranchJob.enqueue(
                instance=self,
                user=request.user if request else None
            )

    def delete(self, *args, **kwargs):
        if active_branch.get() == self:
            raise AbortRequest(_("The active branch cannot be deleted."))

        # Row delete and schema drop must succeed or fail together — see #445.
        with transaction.atomic():
            result = super().delete(*args, **kwargs)
            self.deprovision()

        return result

    def set_backend_id(self, backend_id):
        """
        Assign and persist this Branch's backend identifier. Called by the branching
        backend during provisioning.

        A branch's identifier is immutable once assigned: it is what addresses the
        branch's isolated dataset, so changing it would orphan the data the branch
        already holds. Re-assigning the value already in place is permitted, so that
        a backend need not special-case a retried provision.
        """
        if self.backend_id and self.backend_id != backend_id:
            raise ValueError(
                f"Branch {self} already has a backend ID ({self.backend_id}); a branch's "
                f"backend ID cannot be changed."
            )

        self.backend_id = backend_id

        # Only reachable for a backend whose get_connection_alias() tolerates a missing
        # identifier: schema_name raises without one, and cached_property does not cache
        # a raised exception. Such a backend may have cached an alias derived from None.
        self.__dict__.pop('schema_name', None)
        self.__dict__.pop('connection_name', None)

        # Scoped to the one column, so this cannot write back the stale in-memory status
        # over the transition Branch.provision() applied with a queryset update().
        self.save(update_fields=['backend_id'])

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
        if self.status == BranchStatusChoices.NEW or not self.backend_id:
            return ObjectChange.objects.none()
        return ObjectChange.objects.using(self.connection_name)

    def get_unsynced_changes(self):
        """
        Return a queryset of all ObjectChange records created in main since the Branch was last synced or created.
        """
        # TODO: Remove this fallback logic in a future release
        # Backward compatibility for branches created before v0.5.6, which did not have last_sync set automatically
        # upon provisioning. Defaults to the branch creation time.
        last_sync = self.last_sync or self.created
        if self.status == BranchStatusChoices.READY:
            return ObjectChange.objects.using(DEFAULT_DB_ALIAS).exclude(
                application__branch=self
            ).filter(
                changed_object_type__in=get_branchable_object_types(),
                time__gt=last_sync
            )
        return ObjectChange.objects.none()

    def get_unmerged_changes(self):
        """
        Return a queryset of all unmerged ObjectChange records within the Branch schema.
        """
        if self.status == BranchStatusChoices.READY and self.backend_id:
            return ObjectChange.objects.using(self.connection_name)
        return ObjectChange.objects.none()

    def get_merged_changes(self):
        """
        Return a queryset of all merged ObjectChange records for the Branch.
        """
        if self.status in (BranchStatusChoices.MERGED, BranchStatusChoices.ARCHIVED):
            return ObjectChange.objects.using(DEFAULT_DB_ALIAS).filter(
                application__branch=self
            )
        return ObjectChange.objects.none()

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

    def _days_until_stale(self):
        """
        Return the number of days remaining until the branch becomes stale, or None if indeterminate
        (branch not yet provisioned or changelog retention is disabled). Returns a negative number if
        the branch is already stale.
        """
        if self.last_sync is None:
            return None
        if not (changelog_retention := get_config().CHANGELOG_RETENTION):
            return None
        stale_at = self.last_sync + timedelta(days=changelog_retention)
        return math.ceil((stale_at - timezone.now()).total_seconds() / 86400)

    @property
    def is_stale(self):
        """
        Indicates whether the branch is too far out of date to be synced.
        """
        days = self._days_until_stale()
        return days is not None and days < 0

    @property
    def stale_warning(self):
        """
        Return the number of days remaining until the branch becomes stale if within the warning
        window, else None.
        """
        days = self._days_until_stale()
        if days is None or days <= 0:
            return None
        threshold = get_plugin_config('netbox_branching', 'stale_warning_threshold')
        if not threshold or days > threshold:
            return None
        return days

    #
    # Migration handling
    #

    @cached_property
    def pending_migrations(self):
        """
        Return a list of database migrations which have been applied in main but not in the branch.
        """
        if not self.backend_id:
            # Nothing has been provisioned yet, so nothing can be outstanding
            return []
        return self.backend.get_pending_migrations(self)

    @cached_property
    def migrators(self):
        """
        Return a dictionary mapping object types to a list of migrators to be run when syncing, merging, or
        reverting a Branch.
        """
        migrators = defaultdict(list)
        for migration in self.applied_migrations:
            app_label, name = migration.split('.')

            try:
                module = importlib.import_module(f'{app_label}.migrations.{name}')
            except ModuleNotFoundError:
                logger = logging.getLogger('netbox_branching.branch')
                logger.warning(f"Failed to load module for migration {migration}; skipping.")
                continue

            for object_type, migrator in getattr(module, 'objectchange_migrators', {}).items():
                migrators[object_type].append(migrator)
        return migrators

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
                    return BranchActionIndicator(False, _('Validation failed for %s: %s') % (action, func))
                return indicator

        return BranchActionIndicator(True)

    @cached_property
    def can_sync(self):
        """
        Indicates whether the branch can be synced.
        """
        return self._can_do_action('sync')

    @cached_property
    def can_migrate(self):
        """
        Indicates whether the branch can be migrated.
        """
        return self._can_do_action('migrate')

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
    # Interrupted operation recovery
    #

    def check_stuck(self):
        """
        Return a ``(job, is_stuck)`` tuple describing whether this branch is stuck in a transitional
        status — that is, whether the job responsible for the status is no longer running. `job` is
        the Job which owns the status, or None if no such job record exists. See issue #622.

        A branch operation writes its transitional status to the database before it starts and clears
        it from within the worker process, so a worker which is killed outright (an OOM kill, an
        evicted container, `kill -9`) leaves the branch in that status permanently. The job which
        should have cleared it is identified by the status itself; it is considered no longer running
        if it has already terminated, or if RQ and its elapsed runtime agree that nothing is executing
        it any more.

        A branch whose operation was invoked directly rather than through the job queue has no job to
        report on; because a transitional status must otherwise be cleared by a job, such a branch is
        reported as stuck (with `None` for the job) once no matching job is enqueued for it.
        """
        from netbox_branching.jobs import get_job_class_for_status

        if self.status not in BranchStatusChoices.TRANSITIONAL:
            return None, False

        job_class = get_job_class_for_status(self.status)
        job = self.jobs.filter(name=job_class.Meta.name).order_by('created').last() if job_class else None
        if job is None:
            # No job record survives for this operation (it was purged, or the operation was never
            # queued), so nothing is going to clear the status.
            return None, True

        grace_period = get_plugin_config('netbox_branching', 'stuck_job_grace_period') or 0
        return job, is_job_abandoned(job, grace_period)

    @property
    def is_stuck(self):
        """
        Indicates that the branch is in a transitional status which no running job will ever clear.
        """
        _, stuck = self.check_stuck()
        return stuck

    def recover(self, user=None, retry=False):
        """
        Reset a branch which is stuck in a transitional status, returning the status it was reset to.
        Returns None if the branch is not stuck. Any orphaned job record is marked as failed so that
        it no longer appears to be running.

        Args:
            retry: Re-enqueue the interrupted operation once the status has been reset, so that the
                   operator does not have to initiate it again. Honoured only for the statuses in
                   `BranchStatusChoices.RECOVERY_RETRYABLE`.
        """
        logger = logging.getLogger('netbox_branching.branch.recover')

        job, stuck = self.check_stuck()
        if not stuck:
            return None

        return self._reset_status(job, user, logger, retry=retry)

    recover.alters_data = True

    def force_recover(self, user=None, retry=False):
        """
        Reset a branch in a transitional status regardless of whether its job still appears to be
        running. Intended for the explicit, operator-initiated recovery of a branch whose job records
        are no longer available; prefer recover() everywhere else.
        """
        logger = logging.getLogger('netbox_branching.branch.recover')

        if self.status not in BranchStatusChoices.TRANSITIONAL:
            return None

        job, _ = self.check_stuck()
        return self._reset_status(job, user, logger, retry=retry)

    force_recover.alters_data = True

    def _reset_status(self, job, user, logger, retry=False):
        """
        Perform the status reset for recover()/force_recover(), optionally re-enqueueing the
        interrupted operation. Assumes the branch has already been determined to be in a
        transitional status.
        """
        from netbox_branching.jobs import get_job_class_for_status

        interrupted_status = self.status
        new_status = BranchStatusChoices.RECOVERY_STATUS[interrupted_status]
        logger.warning(
            f"Recovering branch {self} from status '{interrupted_status}': resetting to '{new_status}' "
            f"(requested by {user or 'system'})"
        )

        # Claim the reset before acting on it. The conditional update succeeds for exactly one
        # caller, so two concurrent recoveries of the same branch — two API calls with force=true,
        # or the watchdog racing an operator — cannot each terminate the job and enqueue a retry,
        # leaving two sync or migrate jobs running against the same branch.
        claimed = Branch.objects.filter(pk=self.pk, status=interrupted_status).update(status=new_status)
        self.status = new_status
        if not claimed:
            logger.info(f"Branch {self} was already recovered by another caller; nothing further to do")
            return new_status

        # Terminate the orphaned job record so it no longer reports itself as running. Jobs which
        # have already terminated are left as they are, to preserve their recorded outcome.
        if job is not None and job.status not in JobStatusChoices.TERMINAL_STATE_CHOICES:
            job.terminate(
                status=JobStatusChoices.STATUS_FAILED,
                error=str(_(
                    "The job did not complete. Its worker is no longer running; the branch has been "
                    "reset to '{status}'."
                )).format(status=new_status)
            )

        # Pick the interrupted operation back up rather than leaving the operator to re-initiate it
        # from the reset status. Only the branch-local operations are retried — see RECOVERY_RETRYABLE
        # for why merges, reverts and provisioning are not.
        if retry and interrupted_status in BranchStatusChoices.RECOVERY_RETRYABLE:
            job_class = get_job_class_for_status(interrupted_status)
            logger.info(f"Re-enqueueing {job_class.Meta.name} for branch {self}")
            job_class.enqueue(instance=self, user=user)

        return new_status

    #
    # Branch actions
    #

    def _apply_sync_update(self, change, logger, touched_object_keys, sync_buffer):
        """
        Apply a non-DELETE change from main and, when the branch has also touched the
        same object, buffer the pre/post state so a single synthetic ObjectChange can
        be written per object after the sync loop completes (#28).

        ``touched_object_keys`` is a set of ``(content_type_id, object_id)`` tuples
        for which the branch has at least one ChangeDiff. It is pre-fetched once by
        the caller to avoid an N+1 query inside the sync loop.

        ``sync_buffer`` is a dict keyed by ``(content_type_id, object_id)``. Sequential
        main changes to the same object collapse into a single buffered entry: the
        first prechange is preserved and postchange is overwritten on each hit. Both
        merge strategies replay to the same end state with or without this
        consolidation, so dropping the intermediate synthetic rows is a no-op for
        correctness while reducing ChangeDiff write traffic at flush time.
        """
        content_type_id = change.changed_object_type_id
        object_id = change.changed_object_id

        # If the branch has not touched this object, no merge-time conflict is possible
        if (content_type_id, object_id) not in touched_object_keys:
            change.apply(self, using=self.connection_name, logger=logger, skip_missing=True)
            return

        model_class = change.changed_object_type.model_class()
        key = (content_type_id, object_id)

        # Capture the branch's state before applying main's change
        try:
            before = model_class.objects.using(self.connection_name).get(pk=object_id)
            prechange_data = _serialize_for_sync(before)
        except model_class.DoesNotExist:
            # Branch already removed the object; let apply() handle it via skip_missing
            change.apply(self, using=self.connection_name, logger=logger, skip_missing=True)
            return

        # Apply the change from main onto the branch
        change.apply(self, using=self.connection_name, logger=logger, skip_missing=True)

        # Capture the branch's state after applying main's change
        try:
            after = model_class.objects.using(self.connection_name).get(pk=object_id)
        except model_class.DoesNotExist:
            # Apply turned this into a delete (shouldn't happen for non-DELETE actions,
            # but be defensive). Drop any prior buffered entry for this object.
            sync_buffer.pop(key, None)
            return
        postchange_data = _serialize_for_sync(after)

        if key in sync_buffer:
            # Consolidate: preserve the original prechange, overwrite postchange.
            entry = sync_buffer[key]
            entry['postchange_data'] = postchange_data
            entry['object_repr'] = str(after)
            if change.user_name and change.user_name not in entry['user_names']:
                entry['user_names'].append(change.user_name)
        else:
            sync_buffer[key] = {
                'content_type_id': content_type_id,
                'object_id': object_id,
                'model_class': model_class,
                'object_repr': str(after),
                'prechange_data': prechange_data,
                'postchange_data': postchange_data,
                'user_names': [change.user_name] if change.user_name else [],
            }

    def _flush_sync_buffer(self, sync_buffer, user, request_id, logger):
        """
        Write the consolidated synthetic ObjectChanges from the sync loop to the
        branch schema. Each save fires record_change_diff, which updates ChangeDiff
        on the default DB — keeping these writes batched at the end of sync
        confines the ChangeDiff row locks to a brief flush window instead of
        holding them across the entire sync loop.

        ``record_change_diff`` is registered on the core ObjectChange model, so
        rows must be created via ``ObjectChange_`` rather than the branch-aware
        proxy (Django dispatches proxy post_save with the proxy as sender).
        """
        if not sync_buffer:
            return

        user_name = user.username if user else ''
        message_max_length = ObjectChange_._meta.get_field('message').max_length

        for entry in sync_buffer.values():
            # Drop entries whose consolidated net change is a no-op (e.g. main
            # toggled a value and then toggled it back during this sync window).
            if entry['prechange_data'] == entry['postchange_data']:
                continue

            originals = ', '.join(entry['user_names']) or 'system'
            sync_message = (
                f'Synced from main (originally by {originals})'
            )[:message_max_length]

            ObjectChange_.objects.using(self.connection_name).create(
                action=ObjectChangeActionChoices.ACTION_UPDATE,
                changed_object_type_id=entry['content_type_id'],
                changed_object_id=entry['object_id'],
                object_repr=entry['object_repr'],
                prechange_data=entry['prechange_data'],
                postchange_data=entry['postchange_data'],
                user=user,
                user_name=user_name,
                request_id=request_id,
                message=sync_message,
            )
            logger.debug(
                f'Recorded sync-applied change to {entry["model_class"]._meta.verbose_name} '
                f'{entry["object_repr"]} (supersedes prior branch change on conflicting fields)'
            )

    def _handle_sync_delete(self, change, branchable_models, user, logger, request_id=None):
        """
        Apply a DELETE change to the branch schema and record ObjectChange entries for any
        branch-originated objects that are cascade-deleted as a side effect.

        When a parent object is deleted in main and synced to the branch, child objects that
        exist only in the branch (no record in main) are cascade-deleted at the DB level with
        no corresponding changelog entry. This method captures those deletions via a temporary
        pre_delete signal handler and writes a synthetic DELETE ObjectChange for each one.
        """
        cascade_targets = {}  # keyed by (model, pk) to deduplicate repeated pre_delete signals
        primary_model = change.changed_object_type.model_class()
        primary_pk = change.changed_object_id

        def _capture_cascade(
            sender, instance, using,
            _conn=self.connection_name,
            _primary_model=primary_model,
            _primary_pk=primary_pk,
            _targets=cascade_targets,
            **kwargs,
        ):
            if using != _conn or (sender is _primary_model and instance.pk == _primary_pk):
                return
            if sender not in branchable_models:
                return
            key = (sender, instance.pk)
            if key in _targets:
                return
            if not sender.objects.using(DEFAULT_DB_ALIAS).filter(pk=instance.pk).exists():
                prechange_data = (
                    instance.serialize_object()
                    if hasattr(instance, 'serialize_object')
                    else serialize_object(instance)
                )
                # Capture pk, repr, and model as values now — Django sets instance.pk = None
                # after deletion, so reading them from the instance later would give wrong results.
                _targets[key] = (sender, instance.pk, str(instance), prechange_data)

        uid = f'_capture_cascade_{id(_capture_cascade)}'
        pre_delete.connect(_capture_cascade, weak=False, dispatch_uid=uid)
        try:
            change.apply(self, using=self.connection_name, logger=logger, skip_missing=True)
        finally:
            pre_delete.disconnect(_capture_cascade, dispatch_uid=uid)

        cascade_models = set()
        for model_class, obj_pk, obj_repr, prechange_data in cascade_targets.values():
            cascade_models.add(model_class)
            ct = ContentType.objects.get_for_model(model_class)
            ObjectChange.objects.using(self.connection_name).create(
                action=ObjectChangeActionChoices.ACTION_DELETE,
                changed_object_type=ct,
                changed_object_id=obj_pk,
                object_repr=obj_repr,
                prechange_data=prechange_data,
                postchange_data=None,
                user=user,
                user_name=user.username if user else '',
                request_id=request_id or uuid.uuid4(),
            )
            logger.debug(
                f'Recorded cascade deletion of {model_class._meta.verbose_name} {obj_repr} (branch-originated)'
            )

        return cascade_models

    def sync(self, user, commit=True):
        """
        Apply changes from the main schema onto the Branch's schema.
        """
        logger = logging.getLogger('netbox_branching.branch.sync')
        logger.info(f'Syncing branch {self}')

        if not self.ready:
            raise Exception(f"Branch {self} is not ready to sync")
        if self.is_stale:
            raise Exception(f"Branch {self} is stale and can no longer be synced")
        if commit and not self.can_sync:
            raise Exception("Syncing this branch is not permitted.")

        # Emit pre-sync signal
        pre_sync.send(sender=self.__class__, branch=self, user=user)

        # Retrieve unsynced changes before we update the Branch's status
        if changes := self.get_unsynced_changes().order_by('time'):
            logger.info(f"Found {len(changes)} changes to sync")
        else:
            logger.info("No changes found; aborting.")
            return

        # Update Branch status
        logger.debug(f"Setting branch status to {BranchStatusChoices.SYNCING}")
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.SYNCING)

        # Generate a request ID for correlating ObjectChange records from this sync
        request_id = uuid.uuid4()

        try:
            # The outer DEFAULT_DB_ALIAS atomic exists so that ChangeDiff updates
            # (written via record_change_diff when synthetic ObjectChanges are saved
            # during _flush_sync_buffer) roll back together with the branch-schema
            # writes if AbortTransaction is raised (e.g. commit=False). It costs
            # essentially nothing during the loop itself: no main-DB writes happen
            # until the flush, so no row locks are held on the default DB until then.
            with (
                activate_branch(self),
                transaction.atomic(using=DEFAULT_DB_ALIAS),
                transaction.atomic(using=self.connection_name),
            ):
                models = set()
                branchable_models = {ct.model_class() for ct in get_branchable_object_types()}

                # Pre-fetch the set of objects the branch has touched so _apply_sync_update
                # can do an in-memory check instead of a per-change ChangeDiff.exists() query.
                # _apply_sync_update never creates a new ChangeDiff (it only updates
                # existing ones via record_change_diff), so the set is stable for the loop.
                touched_object_keys = set(
                    ChangeDiff.objects.filter(branch=self).values_list('object_type_id', 'object_id')
                )

                # Buffer synthetic ObjectChange writes during the loop and flush in
                # one batch at the end. Keyed by (content_type_id, object_id) so
                # multiple main updates to the same object collapse to one entry.
                sync_buffer = {}

                # Apply each change from the main schema
                for change in changes:
                    model_class = change.changed_object_type.model_class()
                    models.add(model_class)
                    if change.action == ObjectChangeActionChoices.ACTION_DELETE:
                        cascade_models = self._handle_sync_delete(
                            change, branchable_models, user, logger, request_id=request_id
                        )
                        models.update(cascade_models)
                        # A buffered synthetic UPDATE is invalidated by a later DELETE.
                        sync_buffer.pop(
                            (change.changed_object_type_id, change.changed_object_id), None
                        )
                    else:
                        self._apply_sync_update(change, logger, touched_object_keys, sync_buffer)

                # Flush buffered synthetic ObjectChanges. This is where
                # record_change_diff actually fires and writes to the default DB.
                self._flush_sync_buffer(sync_buffer, user, request_id, logger)

                if not commit:
                    raise AbortTransaction()

                # Perform cleanup tasks
                strategy_class = get_merge_strategy(self.merge_strategy)
                strategy_class()._clean(models)

        except Exception as e:
            if err_message := str(e):
                logger.error(err_message)
            # Restore original branch status
            Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.READY)
            raise

        # Record the branch's last_synced time & update its status
        logger.debug(f"Setting branch status to {BranchStatusChoices.READY}")
        self.last_sync = timezone.now()
        self.status = BranchStatusChoices.READY
        self.save(update_merge_sync_fields=True)

        # Record a branch event for the sync
        logger.debug(f"Recording branch event: {BranchEventTypeChoices.SYNCED}")
        BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.SYNCED)

        # Emit post-sync signal
        post_sync.send(sender=self.__class__, branch=self, user=user)

        logger.info('Syncing completed')

    sync.alters_data = True

    def migrate(self, user):
        """
        Apply any pending database migrations to the branch schema.
        """
        logger = logging.getLogger('netbox_branching.branch.migrate')
        logger.info(f'Migrating branch {self}')

        def migration_progress_callback(action, migration=None, fake=False):
            if action == "apply_start":
                if fake:
                    logger.debug(f"Faking migration {migration} (no branchable models affected)")
                else:
                    logger.info(f"Applying migration {migration}")
            elif action == "apply_success" and migration is not None:
                self.applied_migrations.append(migration)
                # Persist after each migration rather than only at the end. A migration is applied
                # in its own transaction, so one which has succeeded stays applied even if the job
                # is interrupted; recording it immediately keeps applied_migrations consistent with
                # the branch schema when the job never reaches its final save(). See issue #622.
                Branch.objects.filter(pk=self.pk).update(applied_migrations=self.applied_migrations)

        # Emit pre-migration signal
        pre_migrate.send(sender=self.__class__, branch=self, user=user)

        # Set Branch status
        logger.debug(f"Setting branch status to {BranchStatusChoices.MIGRATING}")
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.MIGRATING)

        # Generate migration plan & apply any migrations
        try:
            self.backend.apply_migrations(self, progress_callback=migration_progress_callback)
        except Exception as e:
            if err_message := str(e):
                logger.error(err_message)
            # Mark the branch as failed so it cannot be activated in a partially-migrated
            # state. Migrations already applied have been persisted by the progress callback.
            Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.FAILED)
            self.status = BranchStatusChoices.FAILED
            raise

        # Reset Branch status to ready
        logger.debug(f"Setting branch status to {BranchStatusChoices.READY}")
        self.status = BranchStatusChoices.READY
        self.save(update_merge_sync_fields=True)

        # Record a branch event for the migration
        logger.debug(f"Recording branch event: {BranchEventTypeChoices.MIGRATED}")
        BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.MIGRATED)

        # Emit post-migration signal
        post_migrate.send(sender=self.__class__, branch=self, user=user)

        logger.info('Migration completed')

    migrate.alters_data = True

    def merge(self, user, commit=True):
        """
        Apply all changes in the Branch to the main schema by replaying them in
        chronological order.
        """
        logger = logging.getLogger('netbox_branching.branch.merge')
        logger.info(f'Merging branch {self}')

        if not self.ready:
            raise Exception(f"Branch {self} is not ready to merge")
        if commit and not self.can_merge:
            raise Exception("Merging this branch is not permitted.")

        # Emit pre-merge signal
        pre_merge.send(sender=self.__class__, branch=self, user=user)

        # Retrieve staged changes before we update the Branch's status
        if changes := self.get_unmerged_changes().order_by('time'):
            logger.info(f"Found {len(changes)} changes to merge")
        else:
            logger.info("No changes found; aborting.")
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
                # Get and execute the appropriate merge strategy
                strategy_class = get_merge_strategy(self.merge_strategy)
                logger.debug(f"Merging using {self.merge_strategy} strategy")
                strategy_class().merge(self, changes, request, logger, user)

                if not commit:
                    raise AbortTransaction()

        except Exception as e:
            if err_message := str(e):
                logger.error(err_message)
            # Disconnect signal receiver & restore original branch status
            post_save.disconnect(handler, sender=ObjectChange_)
            Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.READY)
            raise

        # Update the Branch's status to "merged"
        logger.debug(f"Setting branch status to {BranchStatusChoices.MERGED}")
        self.status = BranchStatusChoices.MERGED
        self.merged_time = timezone.now()
        self.merged_by = user
        self.save(update_merge_sync_fields=True)

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
        logger.info(f'Reverting branch {self}')

        if not self.merged:
            raise Exception("Only merged branches can be reverted.")
        if commit and not self.can_revert:
            raise Exception("Reverting this branch is not permitted.")

        # Emit pre-revert signal
        pre_revert.send(sender=self.__class__, branch=self, user=user)

        # Get the merge strategy to determine the correct ordering for changes
        strategy_class = get_merge_strategy(self.merge_strategy)

        # Retrieve applied changes before we update the Branch's status
        if changes := self.get_changes().order_by(strategy_class.revert_changes_ordering):
            logger.info(f"Found {len(changes)} changes to revert")
        else:
            logger.info("No changes found; aborting.")
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
                # Execute the revert strategy
                logger.debug(f"Reverting using {self.merge_strategy} strategy")
                strategy_class().revert(self, changes, request, logger, user)

                if not commit:
                    raise AbortTransaction()

        except Exception as e:
            if err_message := str(e):
                logger.error(err_message)
            # Disconnect signal receiver & restore original branch status
            post_save.disconnect(handler, sender=ObjectChange_)
            Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.MERGED)
            raise

        # Update the Branch's status to "ready"
        logger.debug(f"Setting branch status to {BranchStatusChoices.READY}")
        self.status = BranchStatusChoices.READY
        self.merged_time = None
        self.merged_by = None
        self.merge_strategy = None
        self.save(update_merge_sync_fields=True)

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
        Create the isolated dataset backing this branch by delegating to the configured
        branching backend. On failure the branch is marked FAILED; the backend is
        responsible for cleaning up any partial state it created.
        """
        logger = logging.getLogger('netbox_branching.branch.provision')
        logger.info(f'Provisioning branch {self}')

        # Emit pre-provision signal
        pre_provision.send(sender=self.__class__, branch=self, user=user)

        # Update Branch status
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.PROVISIONING)

        try:
            self.backend.provision(self, user)
        except Exception:
            Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.FAILED)
            raise

        # Emit post-provision signal
        post_provision.send(sender=self.__class__, branch=self, user=user)

        logger.info('Provisioning completed')

        Branch.objects.filter(pk=self.pk).update(
            status=BranchStatusChoices.READY,
            last_sync=timezone.now(),
        )
        BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.PROVISIONED)

    provision.alters_data = True

    def archive(self, user):
        """
        Deprovision the Branch and set its status to "archived."
        """
        if not self.can_archive:
            raise Exception("Archiving this branch is not permitted.")

        # Drop the schema and flip the status atomically so a failure after the schema
        # has been dropped does not leave an un-archived Branch with no schema.
        with transaction.atomic():
            self.deprovision()
            Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.ARCHIVED)
            BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.ARCHIVED)

    archive.alters_data = True

    def deprovision(self):
        """
        Destroy the isolated dataset backing this branch by delegating to the configured
        branching backend.
        """
        logger = logging.getLogger('netbox_branching.branch.provision')
        logger.info(f'Deprovisioning branch {self}')

        # Emit pre-deprovision signal
        pre_deprovision.send(sender=self.__class__, branch=self)

        self.backend.deprovision(self)

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

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('-time',)
        verbose_name = _('branch event')
        verbose_name_plural = _('branch events')

    def get_type_color(self):
        return BranchEventTypeChoices.colors.get(self.type)
