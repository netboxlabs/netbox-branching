import importlib
import logging
import random
import string
from collections import defaultdict
from datetime import timedelta
from functools import cached_property, partial

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connection, connections, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.signals import post_save
from django.db.utils import ProgrammingError
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from mptt.models import MPTTModel

from core.models import ObjectChange as ObjectChange_
from netbox.config import get_config
from netbox.context import current_request
from netbox.context_managers import event_tracking
from netbox.models import PrimaryModel
from netbox.models.features import JobsMixin
from netbox.plugins import get_plugin_config
from netbox_branching.choices import BranchEventTypeChoices, BranchStatusChoices
from netbox_branching.constants import BRANCH_ACTIONS
from netbox_branching.constants import SKIP_INDEXES
from netbox_branching.contextvars import active_branch
from netbox_branching.signals import *
from netbox_branching.utilities import BranchActionIndicator
from netbox_branching.utilities import (
    ChangeSummary, activate_branch, get_branchable_object_types, get_sql_results, get_tables_to_replicate,
    record_applied_change,
)
from utilities.exceptions import AbortRequest, AbortTransaction
from utilities.querysets import RestrictedQuerySet
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

    _preaction_validators = {
        'sync': set(),
        'migrate': set(),
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
        from netbox_branching.jobs import ProvisionBranchJob

        _provision = provision and self.pk is None

        super().save(*args, **kwargs)

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
        if self.status == BranchStatusChoices.READY:
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

    @property
    def is_stale(self):
        """
        Indicates whether the branch is too far out of date to be synced.
        """
        if self.last_sync is None:
            # Branch has not yet been provisioned
            return False
        if not (changelog_retention := get_config().CHANGELOG_RETENTION):
            # Changelog retention is disabled
            return False
        return self.last_sync < timezone.now() - timedelta(days=changelog_retention)

    #
    # Migration handling
    #

    @cached_property
    def pending_migrations(self):
        """
        Return a list of database migrations which have been applied in main but not in the branch.
        """
        connection = connections[self.connection_name]
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
        return [
            (migration.app_label, migration.name) for migration, backward in plan
        ]

    @cached_property
    def migrators(self):
        """
        Return a dictionary mapping object types to a list of migrators to be run when syncing, merging, or
        reverting a Branch.
        """
        migrators = defaultdict(list)
        for migration in self.applied_migrations:
            app_label, name = migration.split('.')
            module = importlib.import_module(f'{app_label}.migrations.{name}')
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

        try:
            with activate_branch(self):
                with transaction.atomic(using=self.connection_name):
                    models = set()

                    # Apply each change from the main schema
                    for change in changes:
                        models.add(change.changed_object_type.model_class())
                        change.apply(self, using=self.connection_name, logger=logger)
                    if not commit:
                        raise AbortTransaction()

                    # Perform cleanup tasks
                    self._cleanup(models)

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

    def migrate(self, user):
        """
        Apply any pending database migrations to the branch schema.
        """
        logger = logging.getLogger('netbox_branching.branch.migrate')
        logger.info(f'Migrating branch {self} ({self.schema_name})')

        def migration_progress_callback(action, migration=None, fake=False):
            if action == "apply_start":
                logger.info(f"Applying migration {migration}")
            elif action == "apply_success" and migration is not None:
                self.applied_migrations.append(migration)

        # Emit pre-migration signal
        pre_migrate.send(sender=self.__class__, branch=self, user=user)

        # Set Branch status
        logger.debug(f"Setting branch status to {BranchStatusChoices.MIGRATING}")
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.MIGRATING)

        # Generate migration plan & apply any migrations
        connection = connections[self.connection_name]
        executor = MigrationExecutor(connection, progress_callback=migration_progress_callback)
        targets = executor.loader.graph.leaf_nodes()
        if plan := executor.migration_plan(targets):
            try:
                # Run migrations
                executor.migrate(targets, plan)
            except Exception as e:
                if err_message := str(e):
                    logger.error(err_message)
                # Save applied migrations & reset status
                self.status = BranchStatusChoices.READY
                self.save()
                raise e
        else:
            logger.info("Found no migrations to apply")

        # Reset Branch status to ready
        logger.debug(f"Setting branch status to {BranchStatusChoices.READY}")
        self.status = BranchStatusChoices.READY
        self.save()

        # Record a branch event for the migration
        logger.debug(f"Recording branch event: {BranchEventTypeChoices.MIGRATED}")
        BranchEvent.objects.create(branch=self, user=user, type=BranchEventTypeChoices.MIGRATED)

        # Emit post-migration signal
        post_migrate.send(sender=self.__class__, branch=self, user=user)

        logger.info('Migration completed')

    migrate.alters_data = True

    # Helper class and functions for collapsing ObjectChanges during merge
    class CollapsedChange:
        """
        Represents a collapsed set of ObjectChanges for a single object.
        """
        def __init__(self, key, model_class):
            self.key = key  # (content_type_id, object_id)
            self.model_class = model_class
            self.changes = []  # List of ObjectChange instances, ordered by time
            self.final_action = None  # 'create', 'update', 'delete', or 'skip'
            self.merged_data = None  # The collapsed postchange_data
            self.last_change = None  # The most recent ObjectChange (for metadata)

            # Dependencies for ordering
            self.depends_on = set()  # Set of keys this change depends on
            self.depended_by = set()  # Set of keys that depend on this change

        def __repr__(self):
            ct_id, obj_id = self.key
            return (
                f"<CollapsedChange {self.model_class.__name__}:{obj_id} "
                f"action={self.final_action} changes={len(self.changes)}>"
            )

    @staticmethod
    def _update_references_creates(update_change, all_changes_by_key):
        """
        Check if an UPDATE references (via FK) any objects being CREATEd in this merge.
        Returns: True if the update has FK references to created objects.
        """
        if not update_change.postchange_data:
            return False

        model_class = update_change.changed_object_type.model_class()

        # Check each FK field
        for field in model_class._meta.get_fields():
            if isinstance(field, models.ForeignKey):
                fk_field_name = field.attname  # e.g., 'device_id'
                fk_value = update_change.postchange_data.get(fk_field_name)

                if fk_value:
                    # Get the key for the referenced object
                    related_model = field.related_model
                    related_ct = ContentType.objects.get_for_model(related_model)
                    ref_key = (related_ct.id, fk_value)

                    # Check if this object is being created
                    if ref_key in all_changes_by_key:
                        # Check if any change for this object is a CREATE
                        for change in all_changes_by_key[ref_key]:
                            if change.action == 'create':
                                return True

        return False

    @staticmethod
    def _collapse_changes_for_object(changes, all_changes_by_key, logger):
        """
        Collapse a list of ObjectChanges for a single object.

        Returns: list of CollapsedChange objects

        Simplified collapsing rules:
        1. If there's a DELETE:
           - If there's also a CREATE: skip entirely (return [])
           - Otherwise: keep only the DELETE (with prechange_data from first ObjectChange)
        2. If no DELETE:
           - If first is CREATE: keep CREATE with postchange_data, plus any UPDATEs
           - If all UPDATEs: collapse intelligently (keep referencing UPDATEs separate)
        """
        if not changes:
            return []

        # Sort by time (oldest first)
        changes = sorted(changes, key=lambda c: c.time)
        model_name = changes[0].changed_object_type.model_class().__name__
        object_id = changes[0].changed_object_id

        logger.debug(f"  Collapsing {len(changes)} changes for {model_name}:{object_id}")

        # Check if there's a DELETE
        has_delete = any(c.action == 'delete' for c in changes)
        has_create = any(c.action == 'create' for c in changes)

        if has_delete:
            if has_create:
                # CREATE + DELETE = skip entirely
                logger.debug("  -> SKIP (created and deleted in branch)")
                return []
            else:
                # Just DELETE (ignore all other changes)
                # Use prechange_data from first ObjectChange
                logger.debug(f"  -> DELETE (keeping only DELETE, ignoring {len(changes) - 1} other changes)")
                delete_change = next(c for c in changes if c.action == 'delete')

                # Copy prechange_data from first change to the delete
                delete_change.prechange_data = changes[0].prechange_data

                result = Branch.CollapsedChange(
                    key=(delete_change.changed_object_type.id, delete_change.changed_object_id),
                    model_class=delete_change.changed_object_type.model_class()
                )
                result.changes = [delete_change]
                result.final_action = 'delete'
                result.merged_data = None
                result.last_change = delete_change
                return [result]

        # No DELETE - handle CREATE or UPDATEs
        result_list = []

        create_changes = [c for c in changes if c.action == 'create']
        update_changes = [c for c in changes if c.action == 'update']

        if create_changes:
            create = create_changes[0]
            logger.debug(f"  -> CREATE (time {create.time})")
            collapsed = Branch.CollapsedChange(
                key=(create.changed_object_type.id, create.changed_object_id),
                model_class=create.changed_object_type.model_class()
            )
            collapsed.changes = [create]
            collapsed.final_action = 'create'
            collapsed.merged_data = create.postchange_data
            collapsed.last_change = create
            result_list.append(collapsed)

        # Process UPDATEs separately (don't merge into CREATE)
        # Keep referencing UPDATEs separate so time ordering respects dependencies
        if update_changes:
            i = 0
            while i < len(update_changes):
                current_update = update_changes[i]

                # Check if this update references a create
                has_ref = Branch._update_references_creates(current_update, all_changes_by_key)

                if has_ref:
                    # Keep referencing update separate
                    logger.debug(f"  -> UPDATE (time {current_update.time}, has FK reference to CREATE)")
                    collapsed = Branch.CollapsedChange(
                        key=(current_update.changed_object_type.id, current_update.changed_object_id),
                        model_class=current_update.changed_object_type.model_class()
                    )
                    collapsed.changes = [current_update]
                    collapsed.final_action = 'update'
                    collapsed.merged_data = current_update.postchange_data
                    collapsed.last_change = current_update
                    result_list.append(collapsed)
                    i += 1
                else:
                    # Collapse consecutive non-referencing updates
                    group_changes = [current_update]

                    # Find consecutive non-referencing updates
                    i += 1
                    while i < len(update_changes):
                        next_update = update_changes[i]
                        if Branch._update_references_creates(next_update, all_changes_by_key):
                            break
                        group_changes.append(next_update)
                        i += 1

                    # Collapse the group
                    merged_data = {}
                    if group_changes[0].prechange_data:
                        merged_data.update(group_changes[0].prechange_data)
                    for change in group_changes:
                        if change.postchange_data:
                            merged_data.update(change.postchange_data)

                    last_change = group_changes[-1]
                    logger.debug(
                        f"  -> UPDATE (time {last_change.time}, "
                        f"collapsed {len(group_changes)} non-referencing updates)"
                    )

                    collapsed = Branch.CollapsedChange(
                        key=(last_change.changed_object_type.id, last_change.changed_object_id),
                        model_class=last_change.changed_object_type.model_class()
                    )
                    collapsed.changes = group_changes
                    collapsed.final_action = 'update'
                    collapsed.merged_data = merged_data
                    collapsed.last_change = last_change
                    result_list.append(collapsed)

        return result_list

    @staticmethod
    def _remove_references_to_skipped_objects(collapsed_changes, skipped_keys, logger):
        """
        Remove FK references to skipped objects (CREATE + DELETE) from collapsed changes.

        For UPDATEs: Safe to remove FK fields since object already exists in valid state.
        The field will remain unchanged (keeps its previous value).

        For CREATEs: If a CREATE references a skipped object, it might fail validation
        if the FK is required (NOT NULL). This is an edge case that should be rare:
        - Object A created
        - Object B created with FK to A
        - Object A deleted
        - Merge attempt: B's CREATE references non-existent A
        TODO: Consider detecting and reporting this case with a clear error message.

        Args:
            collapsed_changes: List of CollapsedChange objects
            skipped_keys: Set of (ct_id, obj_id) tuples for skipped objects
            logger: Logger instance

        Returns:
            Filtered list of CollapsedChange objects (UPDATEs with no changes removed)
        """
        if not skipped_keys:
            return collapsed_changes

        logger.info(f"Checking for references to {len(skipped_keys)} skipped objects...")

        changes_to_remove = []

        for collapsed in collapsed_changes:
            if collapsed.final_action not in ('create', 'update'):
                continue

            if not collapsed.merged_data:
                continue

            # Check each FK field for references to skipped objects
            fields_to_remove = []
            for field in collapsed.model_class._meta.get_fields():
                if isinstance(field, models.ForeignKey):
                    related_model = field.related_model
                    related_ct = ContentType.objects.get_for_model(related_model)

                    # Check both field.name (e.g., 'site') and field.attname (e.g., 'site_id')
                    # ObjectChange may store FK using either name
                    fk_value = None
                    field_name_to_remove = None

                    # Try field.name first (e.g., 'site')
                    if field.name in collapsed.merged_data:
                        fk_value = collapsed.merged_data[field.name]
                        field_name_to_remove = field.name
                    # Try field.attname (e.g., 'site_id')
                    elif field.attname in collapsed.merged_data:
                        fk_value = collapsed.merged_data[field.attname]
                        field_name_to_remove = field.attname

                    if fk_value:
                        ref_key = (related_ct.id, fk_value)

                        if ref_key in skipped_keys:
                            logger.warning(
                                f"  {collapsed} references skipped object "
                                f"{related_model.__name__}:{fk_value}, removing field '{field.name}'"
                            )
                            fields_to_remove.append(field_name_to_remove)

            # Remove the fields that reference skipped objects
            for field_name in fields_to_remove:
                del collapsed.merged_data[field_name]

            # For CREATEs: if we removed a required FK field, skip the CREATE entirely
            if collapsed.final_action == 'create' and fields_to_remove:
                # Check if any removed field was required (NOT NULL)
                for field_name in fields_to_remove:
                    # Find the field definition
                    field_obj = None
                    for field in collapsed.model_class._meta.get_fields():
                        if isinstance(field, models.ForeignKey):
                            if field.name == field_name or field.attname == field_name:
                                field_obj = field
                                break

                    if field_obj and not field_obj.null:
                        logger.warning(
                            f"  {collapsed} requires removed field '{field_obj.name}' "
                            f"which referenced skipped object. Skipping CREATE entirely."
                        )
                        changes_to_remove.append(collapsed)
                        break

            # For UPDATEs: check if there are any remaining changes
            if collapsed.final_action == 'update' and fields_to_remove:
                first_change = collapsed.changes[0]
                prechange = first_change.prechange_data or {}

                # Check if merged_data differs from prechange_data
                has_changes = any(
                    collapsed.merged_data.get(k) != prechange.get(k)
                    for k in collapsed.merged_data.keys()
                )

                if not has_changes:
                    logger.info(
                        f"  {collapsed} has no remaining changes after removing "
                        f"skipped references, removing from merge"
                    )
                    changes_to_remove.append(collapsed)

        # Remove UPDATEs with no remaining changes
        for collapsed in changes_to_remove:
            collapsed_changes.remove(collapsed)

        if changes_to_remove:
            logger.info(f"  Removed {len(changes_to_remove)} UPDATEs with no remaining changes")

        return collapsed_changes

    @staticmethod
    def _order_collapsed_changes(collapsed_changes, logger):
        """
        Order collapsed changes by time.

        With smart collapsing (keeping referencing UPDATEs separate), natural time order
        respects all dependencies:
        - CREATEs come before UPDATEs that reference them (time order from branch)
        - UPDATEs that remove references come before DELETEs (time order from branch)
        - DELETEs free unique constraints before CREATEs claim them (time order from branch)

        Example 1 - Freeing unique constraints:
          Scenario: Site 's1' exists in main. In branch: DELETE Site 's1', CREATE new Site 's1'
          Needed order: DELETE Site 's1' → CREATE new Site 's1'
          (Delete frees the slug before create claims it)

        Example 2 - Dependency chain:
          Scenario: Interface points to Device A. In branch: CREATE Device B, UPDATE Interface (A→B), DELETE Device A
          Needed order: CREATE Device B → UPDATE Interface → DELETE Device A
          (Create Device B first for FK, update to remove reference to A, then delete A)

        Returns: ordered list of CollapsedChange objects sorted by time
        """
        logger.info(f"Ordering {len(collapsed_changes)} collapsed changes...")

        if not collapsed_changes:
            return []

        # Sort by time (oldest first)
        sorted_changes = sorted(collapsed_changes, key=lambda c: c.last_change.time)

        # Log summary
        action_counts = defaultdict(int)
        for collapsed in sorted_changes:
            action_counts[collapsed.final_action] += 1

        logger.info(f"  Ordered {len(sorted_changes)} changes by time: "
                    f"{action_counts['create']} creates, "
                    f"{action_counts['update']} updates, "
                    f"{action_counts['delete']} deletes")

        return sorted_changes

    def _apply_collapsed_change(self, collapsed, using=DEFAULT_DB_ALIAS, logger=None):
        """
        Apply a collapsed change to the database.
        Similar to ObjectChange.apply() but works with collapsed data.
        """
        from utilities.serialization import deserialize_object
        from netbox_branching.utilities import update_object

        logger = logger or logging.getLogger('netbox_branching.branch._apply_collapsed_change')
        model = collapsed.model_class
        object_id = collapsed.key[1]

        # Run data migrators on the last change (to apply any necessary migrations)
        last_change = collapsed.last_change
        last_change.migrate(self)

        # Creating a new object
        if collapsed.final_action == 'create':
            logger.debug(f'  Creating {model._meta.verbose_name} {object_id}')

            if hasattr(model, 'deserialize_object'):
                instance = model.deserialize_object(collapsed.merged_data, pk=object_id)
            else:
                instance = deserialize_object(model, collapsed.merged_data, pk=object_id)

            try:
                instance.object.full_clean()
            except (FileNotFoundError) as e:
                # If a file was deleted later in this branch it will fail here
                # so we need to ignore it. We can assume the NetBox state is valid.
                logger.warning(f'  Ignoring missing file: {e}')
            instance.save(using=using)

        # Modifying an object
        elif collapsed.final_action == 'update':
            logger.debug(f'  Updating {model._meta.verbose_name} {object_id}')

            try:
                instance = model.objects.using(using).get(pk=object_id)
            except model.DoesNotExist:
                logger.error(f'  {model._meta.verbose_name} {object_id} not found for update')
                raise

            # Calculate what fields changed from the collapsed changes
            # We need to figure out what changed between initial and final state
            first_change = collapsed.changes[0]
            initial_data = first_change.prechange_data or {}
            final_data = collapsed.merged_data or {}

            # Only update fields that actually changed
            changed_fields = {}
            for key, final_value in final_data.items():
                initial_value = initial_data.get(key)
                if initial_value != final_value:
                    changed_fields[key] = final_value

            logger.debug(f'    Updating {len(changed_fields)} fields: {list(changed_fields.keys())}')
            update_object(instance, changed_fields, using=using)

        # Deleting an object
        elif collapsed.final_action == 'delete':
            logger.debug(f'  Deleting {model._meta.verbose_name} {object_id}')

            try:
                instance = model.objects.using(using).get(pk=object_id)
                instance.delete(using=using)
            except model.DoesNotExist:
                logger.debug(f'  {model._meta.verbose_name} {object_id} already deleted; skipping')

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
                models = set()

                # Group changes by object
                logger.info("Grouping ObjectChanges by object...")
                changes_by_object = {}

                for change in changes:
                    key = (change.changed_object_type.id, change.changed_object_id)

                    if key not in changes_by_object:
                        changes_by_object[key] = []
                        model_name = change.changed_object_type.model_class().__name__
                        logger.debug(f"New object: {model_name}:{change.changed_object_id}")

                    changes_by_object[key].append(change)

                logger.info(f"  {len(changes)} changes grouped into {len(changes_by_object)} objects")

                # Collapse each object's changes
                logger.info("Collapsing changes for each object...")
                all_collapsed = []
                skipped_keys = set()

                for key, object_changes in changes_by_object.items():
                    # Returns list of CollapsedChange objects (may be empty if skipped, or multiple for UPDATEs)
                    collapsed_list = Branch._collapse_changes_for_object(
                        object_changes, changes_by_object, logger
                    )

                    # Track skipped objects (CREATE + DELETE)
                    if not collapsed_list:  # Empty list means object was skipped
                        skipped_keys.add(key)
                        logger.debug(f"  Skipped object: {key}")

                    all_collapsed.extend(collapsed_list)

                logger.info(f"  {len(changes)} original changes collapsed into {len(all_collapsed)} operations")
                if skipped_keys:
                    logger.info(f"  {len(skipped_keys)} objects skipped (created and deleted in branch)")

                # Remove references to skipped objects
                all_collapsed = Branch._remove_references_to_skipped_objects(
                    all_collapsed, skipped_keys, logger
                )

                # Order collapsed changes by time
                ordered_changes = Branch._order_collapsed_changes(all_collapsed, logger)

                # Apply collapsed changes in order
                logger.info(f"Applying {len(ordered_changes)} collapsed changes...")
                for i, collapsed in enumerate(ordered_changes, 1):
                    model_class = collapsed.model_class
                    models.add(model_class)

                    # Use the last change's metadata for tracking
                    last_change = collapsed.last_change

                    logger.info(f"  [{i}/{len(ordered_changes)}] {collapsed.final_action.upper()} "
                               f"{model_class.__name__}:{collapsed.key[1]} "
                               f"(from {len(collapsed.changes)} original changes)")

                    with event_tracking(request):
                        request.id = last_change.request_id
                        request.user = last_change.user

                        # Apply the collapsed change
                        self._apply_collapsed_change(collapsed, using=DEFAULT_DB_ALIAS, logger=logger)

                if not commit:
                    raise AbortTransaction()

                # Perform cleanup tasks
                self._cleanup(models)

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
            raise Exception("Only merged branches can be reverted.")
        if commit and not self.can_revert:
            raise Exception("Reverting this branch is not permitted.")

        # Emit pre-revert signal
        pre_revert.send(sender=self.__class__, branch=self, user=user)

        # Retrieve applied changes before we update the Branch's status
        if changes := self.get_changes().order_by('-time'):
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
                models = set()

                # Undo each change from the Branch
                for change in changes:
                    models.add(change.changed_object_type.model_class())
                    with event_tracking(request):
                        request.id = change.request_id
                        request.user = change.user
                        change.undo(self, logger=logger)
                if not commit:
                    raise AbortTransaction()

                # Perform cleanup tasks
                self._cleanup(models)

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

    def _cleanup(self, models):
        """
        Called after syncing, merging, or reverting a branch.
        """
        logger = logging.getLogger('netbox_branching.branch')

        for model in models:

            # Recalculate MPTT as needed
            if issubclass(model, MPTTModel):
                logger.debug(f"Recalculating MPTT for model {model}")
                model.objects.rebuild()

    def provision(self, user):
        """
        Create the schema & replicate main tables.
        """
        logger = logging.getLogger('netbox_branching.branch.provision')
        logger.info(f'Provisioning branch {self} ({self.schema_name})')
        main_schema = get_plugin_config('netbox_branching', 'main_schema')

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
                main_table = f'{main_schema}.{table}'
                schema_table = f'{schema}.{table}'
                logger.debug(f'Creating table {schema_table}')
                cursor.execute(
                    f"CREATE TABLE {schema_table} ( LIKE {main_table} INCLUDING INDEXES )"
                )
                # Set the default value for the ID column to the sequence associated with the source table
                sequence_name = f'{main_schema}.{table}_id_seq'
                cursor.execute(
                    f"ALTER TABLE {schema_table} ALTER COLUMN id SET DEFAULT nextval(%s)", [sequence_name]
                )

                # Copy the migrations table
                main_table = f'{main_schema}.django_migrations'
                schema_table = f'{schema}.django_migrations'
                logger.debug(f'Creating table {schema_table}')
                cursor.execute(
                    f"CREATE TABLE {schema_table} ( LIKE {main_table} INCLUDING INDEXES )"
                )
                cursor.execute(
                    f"INSERT INTO {schema_table} SELECT * FROM {main_table}"
                )
                # Designate id as an identity column
                cursor.execute(
                    f"ALTER TABLE {schema_table} ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY"
                )
                # Set the next value for the ID sequence
                cursor.execute(
                    f"SELECT MAX(id) from {schema_table}"
                )
                starting_id = cursor.fetchone()[0] + 1
                cursor.execute(
                    f"ALTER SEQUENCE {schema}.django_migrations_id_seq RESTART WITH {starting_id}"
                )

                # Replicate relevant tables from the main schema
                for table in get_tables_to_replicate():
                    main_table = f'{main_schema}.{table}'
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

                    # Get the name of the sequence used for object ID allocations (if one exists)
                    cursor.execute(
                        "SELECT pg_get_serial_sequence(%s, 'id')", [table]
                    )
                    # Set the default value for the ID column to the sequence associated with the source table
                    if sequence_name := cursor.fetchone()[0]:
                        cursor.execute(
                            f"ALTER TABLE {schema_table} ALTER COLUMN id SET DEFAULT nextval(%s)", [sequence_name]
                        )

                # Rename indexes to ensure consistency with the main schema for migration compatibility
                cursor.execute(
                    f"SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = '{schema}'"
                )
                for index in get_sql_results(cursor):
                    # Skip duplicate indexes
                    # TODO: Remove in v0.6.0
                    if index.indexname in SKIP_INDEXES:
                        continue

                    # Find the matching index in main based on its table & definition
                    definition = index.indexdef.split(' USING ', maxsplit=1)[1]
                    cursor.execute(
                        "SELECT indexname FROM pg_indexes WHERE schemaname=%s AND tablename=%s AND indexdef LIKE %s",
                        [main_schema, index.tablename, f'% {definition}']
                    )
                    if result := cursor.fetchone():
                        # Rename the branch schema index (if needed)
                        new_name = result[0]
                        if new_name != index.indexname:
                            sql = f"ALTER INDEX {schema}.{index.indexname} RENAME TO {new_name}"
                            try:
                                cursor.execute(sql)
                                logger.debug(sql)
                            except Exception as e:
                                logger.error(sql)
                                raise e
                    else:
                        logger.warning(
                            f"Found no matching index in main for branch index {index.indexname}."
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
