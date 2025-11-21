import importlib
import logging
import random
import string
from collections import defaultdict
from datetime import timedelta
from functools import cached_property, partial

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
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

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange as ObjectChange_
from utilities.data import shallow_compare_dict
from utilities.serialization import deserialize_object
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
    merged_using_collapsed = models.BooleanField(
        verbose_name=_('merged using collapsed'),
        default=False,
        help_text=_('Whether the merge was performed using the collapsed strategy')
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
            self.prechange_data = None  # The original state (from first ObjectChange)
            self.postchange_data = None  # The final state (collapsed from all changes)
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
    def _diff_object_change_data(action, prechange_data, postchange_data):
        """
        Compute diff between prechange_data and postchange_data for a given action.
        Mirrors the logic of ObjectChange.diff() method.

        Returns: dict with 'pre' and 'post' keys containing changed attributes
        """
        prechange_data = prechange_data or {}
        postchange_data = postchange_data or {}

        # Determine which attributes have changed
        if action == ObjectChangeActionChoices.ACTION_CREATE:
            changed_attrs = sorted(postchange_data.keys())
        elif action == ObjectChangeActionChoices.ACTION_DELETE:
            changed_attrs = sorted(prechange_data.keys())
        else:
            # TODO: Support deep (recursive) comparison
            changed_data = shallow_compare_dict(prechange_data, postchange_data)
            changed_attrs = sorted(changed_data.keys())

        return {
            'pre': {
                k: prechange_data.get(k) for k in changed_attrs
            },
            'post': {
                k: postchange_data.get(k) for k in changed_attrs
            },
        }

    @staticmethod
    def _collapse_changes_for_object(changes, logger):
        """
        Collapse a list of ObjectChanges for a single object.
        Returns: (final_action, prechange_data, postchange_data, last_change)

        A key point is that we only care about the final state of the object. Also
        each ChangeObject needs to be correct so the final state is correct, i.e.
        if we delete an object, there aren't going to be other objects still referencing it.

        ChangeObject can have CREATE, UPDATE, and DELETE actions.
        We need to collapse these changes into a single action.
        We can have the following cases:
           - CREATE + (any updates) + DELETE = skip entirely
           - (anything other than CREATE) + DELETE = DELETE
           - CREATE + UPDATEs = CREATE
           - multiple UPDATEs = UPDATE

        """
        if not changes:
            return None, None, None, None

        # Sort by time (oldest first)
        changes = sorted(changes, key=lambda c: c.time)

        # Check if there's a DELETE anywhere in the changes
        has_delete = any(c.action == 'delete' for c in changes)
        has_create = any(c.action == 'create' for c in changes)

        logger.debug(f"  Collapsing {len(changes)} changes...")

        if has_delete:
            if has_create:
                # CREATE + DELETE = skip entirely
                logger.debug("  -> Action: SKIP (created and deleted in branch)")
                return 'skip', None, None, changes[-1]
            else:
                # Just DELETE (ignore all other changes like updates)
                # prechange_data: original state from first change
                # postchange_data: postchange_data from DELETE ObjectChange
                logger.debug(f"  -> Action: DELETE (keeping only DELETE, ignoring {len(changes) - 1} other changes)")
                delete_change = next(c for c in changes if c.action == 'delete')
                prechange_data = changes[0].prechange_data
                postchange_data = delete_change.postchange_data  # Should be None for DELETE, but use actual value
                return 'delete', prechange_data, postchange_data, delete_change

        # No DELETE - handle CREATE or UPDATEs
        first_action = changes[0].action
        first_change = changes[0]
        last_change = changes[-1]

        # Created (with possible updates) -> single create
        if first_action == 'create':
            # prechange_data: from first ObjectChange (should be None for CREATE)
            # postchange_data: merged from all changes
            prechange_data = first_change.prechange_data
            postchange_data = {}
            for change in changes:
                # Merge postchange_data, later changes overwrite earlier ones
                if change.postchange_data:
                    postchange_data.update(change.postchange_data)
            logger.debug(f"  -> Action: CREATE (collapsed {len(changes)} changes)")
            return 'create', prechange_data, postchange_data, last_change

        # Only updates -> single update
        # prechange_data: original state from first change
        # postchange_data: final state after all updates
        prechange_data = first_change.prechange_data
        postchange_data = {}
        # Start with prechange_data of first change as baseline
        if prechange_data:
            postchange_data.update(prechange_data)
        # Apply each change's postchange_data
        for change in changes:
            if change.postchange_data:
                postchange_data.update(change.postchange_data)

        logger.debug(f"  -> Action: UPDATE (collapsed {len(changes)} changes)")
        return 'update', prechange_data, postchange_data, last_change

    @staticmethod
    def _get_fk_references(model_class, data, changed_objects):
        """
        Find foreign key references in the given data that point to objects in changed_objects.
        Returns a set of (content_type_id, object_id) tuples.
        """
        if not data:
            return set()

        references = set()
        for field in model_class._meta.get_fields():
            if isinstance(field, models.ForeignKey):
                fk_value = data.get(field.name)

                if fk_value:
                    # Get the content type of the related model
                    related_model = field.related_model
                    related_ct = ContentType.objects.get_for_model(related_model)
                    ref_key = (related_ct.id, fk_value)

                    # Only track if this object is in our changed_objects
                    if ref_key in changed_objects:
                        references.add(ref_key)

        return references

    @staticmethod
    def _build_fk_dependency_graph(deletes, updates, creates, logger):
        """
        Build the FK dependency graph between collapsed changes.

        Analyzes foreign key references in the data to determine which changes depend on others:
        - UPDATEs that remove FK references must happen before DELETEs
        - UPDATEs that add FK references must happen after CREATEs
        - CREATEs that reference other created objects must happen after those CREATEs
        - DELETEs of child objects must happen before DELETEs of parent objects

        Modifies the CollapsedChange objects in place by setting their depends_on and depended_by sets.
        """
        # Build lookup maps for efficient dependency checking
        deletes_map = {c.key: c for c in deletes}
        creates_map = {c.key: c for c in creates}

        # 1. Check UPDATEs for dependencies
        logger.debug("  Analyzing UPDATE dependencies...")
        for update in updates:
            # Check if UPDATE references deleted object in prechange_data
            # This means the UPDATE had a reference that it's removing
            # The UPDATE must happen BEFORE the DELETE so the FK reference is removed first
            if update.changes[0].prechange_data:
                prechange_refs = Branch._get_fk_references(
                    update.model_class,
                    update.changes[0].prechange_data,
                    deletes_map.keys()
                )
                for ref_key in prechange_refs:
                    # DELETE depends on UPDATE (UPDATE removes reference, then DELETE can proceed)
                    delete_collapsed = deletes_map[ref_key]
                    delete_collapsed.depends_on.add(update.key)
                    update.depended_by.add(ref_key)
                    logger.debug(
                        f"    {delete_collapsed} depends on {update} "
                        f"(UPDATE removes FK reference before DELETE)"
                    )

            # Check if UPDATE references created object in postchange_data
            # This means the UPDATE needs the CREATE to exist first
            # The CREATE must happen BEFORE the UPDATE
            if update.postchange_data:
                postchange_refs = Branch._get_fk_references(
                    update.model_class,
                    update.postchange_data,
                    creates_map.keys()
                )
                for ref_key in postchange_refs:
                    # UPDATE depends on CREATE
                    create_collapsed = creates_map[ref_key]
                    update.depends_on.add(ref_key)
                    create_collapsed.depended_by.add(update.key)
                    logger.debug(
                        f"    {update} depends on {create_collapsed} "
                        f"(UPDATE references created object)"
                    )

        # 2. Check CREATEs for dependencies on other CREATEs
        logger.debug("  Analyzing CREATE dependencies...")
        for create in creates:
            if create.postchange_data:
                # Check if this CREATE references other created objects
                refs = Branch._get_fk_references(
                    create.model_class,
                    create.postchange_data,
                    creates_map.keys()
                )
                for ref_key in refs:
                    if ref_key != create.key:  # Don't self-reference
                        # CREATE depends on another CREATE
                        ref_create = creates_map[ref_key]
                        create.depends_on.add(ref_key)
                        ref_create.depended_by.add(create.key)
                        logger.debug(
                            f"    {create} depends on {ref_create} "
                            f"(CREATE references another created object)"
                        )

        # 3. Check DELETEs for dependencies on other DELETEs
        logger.debug("  Analyzing DELETE dependencies...")
        for delete in deletes:
            if delete.prechange_data:
                # Check if this DELETE references other deleted objects
                refs = Branch._get_fk_references(
                    delete.model_class,
                    delete.prechange_data,
                    deletes_map.keys()
                )
                for ref_key in refs:
                    if ref_key != delete.key:  # Don't self-reference
                        # This delete references another deleted object
                        # The referenced object must be deleted AFTER this one (child before parent)
                        ref_delete = deletes_map[ref_key]
                        ref_delete.depends_on.add(delete.key)
                        delete.depended_by.add(ref_key)
                        logger.debug(
                            f"    {ref_delete} depends on {delete} "
                            f"(child DELETE must happen before parent DELETE)"
                        )

    @staticmethod
    def _dependency_order_by_references(collapsed_changes, logger):
        """
        Orders collapsed changes using topological sort with cycle detection.

        Uses Kahn's algorithm to order nodes respecting their dependency graph.
        Reference: https://en.wikipedia.org/wiki/Topological_sorting#Kahn's_algorithm

        The algorithm processes nodes in "layers" - first all nodes with no dependencies,
        then all nodes whose dependencies have been satisfied, and so on.

        When multiple nodes have no dependencies (equal priority in the dependency graph),
        they are ordered by action type priority: DELETE (0) -> UPDATE (1) -> CREATE (2).
        This maintains the action type grouping when there are no explicit FK dependencies.

        If cycles are detected, breaks them and continues, returning a partial order.

        Returns: ordered list of keys
        """
        logger.info("Adjusting ordering by references...")

        # Define action priority (lower number = higher priority = processed first)
        action_priority = {
            'delete': 0,  # DELETEs should happen first
            'update': 1,  # UPDATEs in the middle
            'create': 2,  # CREATEs should happen last
            'skip': 3,    # SKIPs should never be in the sort, but just in case
        }

        # Create a copy of dependencies to modify
        remaining = {key: set(collapsed.depends_on) for key, collapsed in collapsed_changes.items()}
        ordered = []

        iteration = 0
        max_iterations = len(remaining) * 2  # Safety limit

        while remaining and iteration < max_iterations:
            iteration += 1

            # Find all nodes with no dependencies
            ready = [key for key, deps in remaining.items() if not deps]

            if not ready:
                # No nodes without dependencies - we have a cycle
                logger.error("  Cycle detected in dependency graph.")

                # Log details about the nodes involved in the cycle
                for key, deps in list(remaining.items())[:5]:  # Show first 5 nodes
                    collapsed = collapsed_changes[key]
                    model_name = collapsed.model_class.__name__
                    obj_id = collapsed.key[1]
                    action = collapsed.final_action.upper()

                    # Try to get identifying info
                    data = collapsed.postchange_data or collapsed.prechange_data or {}
                    identifying_info = []
                    for field in ['name', 'slug', 'label']:
                        if field in data:
                            identifying_info.append(f"{field}={data[field]!r}")
                    info_str = f" ({', '.join(identifying_info)})" if identifying_info else ""

                    logger.error(f"    {action} {model_name} (ID: {obj_id}){info_str} depends on: {deps}")

                if len(remaining) > 5:
                    logger.error(f"    ... and {len(remaining) - 5} more nodes in cycle")

                raise Exception(
                    f"Cycle detected in dependency graph. {len(remaining)} changes are involved in "
                    f"circular dependencies and cannot be ordered. Check the logs above for details."
                )
            else:
                # Sort ready nodes by action priority (primary) and time (secondary)
                # This maintains DELETE -> UPDATE -> CREATE ordering, with time ordering within each group
                ready.sort(key=lambda k: (
                    action_priority.get(collapsed_changes[k].final_action, 99),
                    collapsed_changes[k].last_change.time
                ))

            # Process ready nodes
            for key in ready:
                ordered.append(key)
                del remaining[key]

                # Remove this key from other nodes' dependencies
                for deps in remaining.values():
                    deps.discard(key)

        if iteration >= max_iterations:
            logger.error("  Ordering by references exceeded maximum iterations. Possible complex cycle.")

            # Log details about the remaining unprocessed nodes
            for key, deps in list(remaining.items())[:5]:  # Show first 5 nodes
                collapsed = collapsed_changes[key]
                model_name = collapsed.model_class.__name__
                obj_id = collapsed.key[1]
                action = collapsed.final_action.upper()

                # Try to get identifying info
                data = collapsed.postchange_data or collapsed.prechange_data or {}
                identifying_info = []
                for field in ['name', 'slug', 'label']:
                    if field in data:
                        identifying_info.append(f"{field}={data[field]!r}")
                info_str = f" ({', '.join(identifying_info)})" if identifying_info else ""

                logger.error(f"    {action} {model_name} (ID: {obj_id}){info_str} still depends on: {deps}")

            if len(remaining) > 5:
                logger.error(f"    ... and {len(remaining) - 5} more unprocessed nodes")

            raise Exception(
                f"Ordering by references exceeded maximum iterations ({max_iterations}). "
                f"{len(remaining)} changes could not be ordered, possibly due to a complex cycle. "
                f"Check the logs above for details."
            )

        logger.info(f"  Ordering by references completed: {len(ordered)} changes ordered")
        return ordered

    @staticmethod
    def _order_collapsed_changes(collapsed_changes, logger):
        """
        Order collapsed changes respecting dependencies and time.

        Algorithm:
        1. Initial ordering by time: DELETEs, UPDATEs, CREATEs (each group sorted by time)
        2. Build dependency graph:
           - If UPDATE references deleted object in prechange_data → UPDATE must come before DELETE
             (UPDATE removes the FK reference, allowing the DELETE to proceed)
           - If UPDATE references created object in postchange_data → CREATE must come before UPDATE
             (CREATE must exist before UPDATE can reference it)
           - If CREATE references another created object in postchange_data → referenced CREATE must come first
             (Referenced object must exist before referencing object is created)
        3. Topological sort respecting dependencies

        This ensures:
        - DELETEs generally happen first to free unique constraints (time order within group)
        - UPDATEs that remove FK references happen before their associated DELETEs
        - CREATEs happen before UPDATEs/CREATEs that reference them

        Returns: ordered list of CollapsedChange objects
        """
        logger.info(f"Ordering {len(collapsed_changes)} collapsed changes...")

        # Remove skipped objects
        to_process = {k: v for k, v in collapsed_changes.items() if v.final_action != 'skip'}
        skipped = [v for v in collapsed_changes.values() if v.final_action == 'skip']

        logger.info(f"  {len(skipped)} changes will be skipped (created and deleted in branch)")
        logger.info(f"  {len(to_process)} changes to process")

        if not to_process:
            return []

        # Group by action and sort each group by time
        deletes = sorted(
            [v for v in to_process.values() if v.final_action == 'delete'],
            key=lambda c: c.last_change.time
        )
        updates = sorted(
            [v for v in to_process.values() if v.final_action == 'update'],
            key=lambda c: c.last_change.time
        )
        creates = sorted(
            [v for v in to_process.values() if v.final_action == 'create'],
            key=lambda c: c.last_change.time
        )

        logger.info(
            f"  Initial time-based groups: {len(deletes)} deletes, "
            f"{len(updates)} updates, {len(creates)} creates"
        )

        # Reset dependencies
        for collapsed in to_process.values():
            collapsed.depends_on = set()
            collapsed.depended_by = set()

        logger.info("Building dependency graph...")
        Branch._build_fk_dependency_graph(deletes, updates, creates, logger)

        total_deps = sum(len(c.depends_on) for c in to_process.values())
        logger.info(f"  Dependency graph built: {total_deps} dependencies")

        # Topological sort to respect dependencies
        ordered_keys = Branch._dependency_order_by_references(to_process, logger)

        # Convert keys back to collapsed changes
        result = [to_process[key] for key in ordered_keys]

        logger.info(f"Ordering complete: {len(result)} changes to apply")
        return result

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
                instance = model.deserialize_object(collapsed.postchange_data, pk=object_id)
            else:
                instance = deserialize_object(model, collapsed.postchange_data, pk=object_id)

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
            final_data = collapsed.postchange_data or {}

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

    def _undo_collapsed_change(self, collapsed, using=DEFAULT_DB_ALIAS, logger=None):
        """
        Undo a collapsed change from the database (reverse of apply).
        Follows the same pattern as ObjectChange.undo().
        """
        from django.contrib.contenttypes.fields import GenericForeignKey
        from utilities.serialization import deserialize_object
        from netbox_branching.utilities import update_object
        from core.choices import ObjectChangeActionChoices

        logger = logger or logging.getLogger('netbox_branching.branch._undo_collapsed_change')
        model = collapsed.model_class
        object_id = collapsed.key[1]

        # Run data migrators on the last change (in revert mode)
        last_change = collapsed.last_change
        last_change.migrate(self, revert=True)

        # Undoing a CREATE: delete the object
        if collapsed.final_action == 'create':
            logger.debug(f'  Undoing creation of {model._meta.verbose_name} {object_id}')
            try:
                instance = model.objects.using(using).get(pk=object_id)
                instance.delete(using=using)
            except model.DoesNotExist:
                logger.debug(f'  {model._meta.verbose_name} {object_id} does not exist; skipping')

        # Undoing an UPDATE: revert to the original state
        elif collapsed.final_action == 'update':
            logger.debug(f'  Undoing update of {model._meta.verbose_name} {object_id}')

            try:
                instance = model.objects.using(using).get(pk=object_id)
                # Compute diff and apply 'pre' values (like ObjectChange.undo() does)
                diff = Branch._diff_object_change_data(
                    ObjectChangeActionChoices.ACTION_UPDATE,
                    collapsed.prechange_data,
                    collapsed.postchange_data
                )
                update_object(instance, diff['pre'], using=using)
            except model.DoesNotExist:
                logger.debug(f'  {model._meta.verbose_name} {object_id} does not exist; skipping')

        # Undoing a DELETE: restore the object
        elif collapsed.final_action == 'delete':
            logger.debug(f'  Undoing deletion (restoring) {model._meta.verbose_name} {object_id}')

            prechange_data = collapsed.prechange_data or {}

            # Restore from prechange_data (like ObjectChange.undo() does)
            deserialized = deserialize_object(model, prechange_data, pk=object_id)
            instance = deserialized.object

            # Restore GenericForeignKey fields
            for field in instance._meta.private_fields:
                if isinstance(field, GenericForeignKey):
                    ct_field = getattr(instance, field.ct_field)
                    fk_field = getattr(instance, field.fk_field)
                    if ct_field and fk_field:
                        setattr(instance, field.name, ct_field.get_object_for_this_type(pk=fk_field))

            instance.full_clean()
            instance.save(using=using)

    def _merge_iterative(self, changes, request, commit, logger):
        """
        Apply changes iteratively (one at a time) in chronological order.
        """
        models = set()

        # Apply each change from the Branch
        for change in changes:
            models.add(change.changed_object_type.model_class())
            with event_tracking(request):
                request.id = change.request_id
                request.user = change.user
                change.apply(self, using=DEFAULT_DB_ALIAS, logger=logger)
        if not commit:
            raise AbortTransaction()

        # Perform cleanup tasks
        self._cleanup(models)

    def _merge_collapsed(self, changes, request, commit, logger):
        """
        Apply changes after collapsing them by object and ordering by dependencies.
        """
        models = set()

        # Group and collapse changes by object
        logger.info("Collapsing ObjectChanges by object...")
        collapsed_changes = {}

        for change in changes:
            key = (change.changed_object_type.id, change.changed_object_id)

            if key not in collapsed_changes:
                model_class = change.changed_object_type.model_class()
                collapsed = Branch.CollapsedChange(key, model_class)
                collapsed_changes[key] = collapsed
                logger.debug(f"New object: {model_class.__name__}:{change.changed_object_id}")

            collapsed_changes[key].changes.append(change)

        logger.info(f"  {len(changes)} changes collapsed into {len(collapsed_changes)} objects")

        # Collapse each object's changes
        logger.info("Determining final action for each object...")
        for key, collapsed in collapsed_changes.items():
            final_action, prechange_data, postchange_data, last_change = Branch._collapse_changes_for_object(
                collapsed.changes, logger
            )
            collapsed.final_action = final_action
            collapsed.prechange_data = prechange_data
            collapsed.postchange_data = postchange_data
            collapsed.last_change = last_change

        # Order collapsed changes based on dependencies
        ordered_changes = Branch._order_collapsed_changes(collapsed_changes, logger)

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
                # Choose merge strategy based on merged_using_collapsed setting
                if self.merged_using_collapsed:
                    logger.debug("Merging using collapsed strategy")
                    self._merge_collapsed(changes, request, commit, logger)
                else:
                    logger.debug("Merging using iterative strategy")
                    self._merge_iterative(changes, request, commit, logger)

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

    def _revert_iterative(self, changes, request, commit, logger):
        """
        Undo changes iteratively (one at a time) in reverse chronological order.
        """
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

    def _revert_collapsed(self, changes, request, commit, logger):
        """
        Undo changes after collapsing them by object and ordering by dependencies.
        """
        models = set()

        # Group changes by object and create CollapsedChange objects
        logger.info("Collapsing ObjectChanges by object...")
        collapsed_changes = {}

        for change in changes:
            key = (change.changed_object_type.id, change.changed_object_id)

            if key not in collapsed_changes:
                model_class = change.changed_object_type.model_class()
                collapsed = Branch.CollapsedChange(key, model_class)
                collapsed_changes[key] = collapsed
                logger.debug(f"New object: {model_class.__name__}:{change.changed_object_id}")

            collapsed_changes[key].changes.append(change)

        logger.info(f"  {len(changes)} changes collapsed into {len(collapsed_changes)} objects")

        # Collapse each object's changes
        logger.info("Determining final action for each object...")
        for key, collapsed in collapsed_changes.items():
            final_action, prechange_data, postchange_data, last_change = Branch._collapse_changes_for_object(
                collapsed.changes, logger
            )
            collapsed.final_action = final_action
            collapsed.prechange_data = prechange_data
            collapsed.postchange_data = postchange_data
            collapsed.last_change = last_change

        # Order collapsed changes for revert (reverse of merge order)
        merge_order = Branch._order_collapsed_changes(collapsed_changes, logger)
        ordered_changes = list(reversed(merge_order))

        # Undo collapsed changes in dependency order
        logger.info(f"Undoing {len(ordered_changes)} collapsed changes in dependency order...")
        for i, collapsed in enumerate(ordered_changes, 1):
            model_class = collapsed.model_class
            models.add(model_class)

            # Use the last change's metadata for tracking
            last_change = collapsed.last_change
            logger.info(
                f"[{i}/{len(ordered_changes)}] Undoing {collapsed.final_action} "
                f"{model_class._meta.verbose_name} (ID: {collapsed.key[1]})"
            )

            with event_tracking(request):
                request.id = last_change.request_id
                request.user = last_change.user
                self._undo_collapsed_change(collapsed, using=DEFAULT_DB_ALIAS, logger=logger)

        if not commit:
            raise AbortTransaction()

        # Perform cleanup tasks
        self._cleanup(models)

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
                # Choose revert strategy based on merged_using_collapsed setting
                if self.merged_using_collapsed:
                    logger.debug("Reverting using collapsed strategy")
                    self._revert_collapsed(changes, request, commit, logger)
                else:
                    logger.debug("Reverting using iterative strategy")
                    self._revert_iterative(changes, request, commit, logger)

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
        self.merged_using_collapsed = False
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
