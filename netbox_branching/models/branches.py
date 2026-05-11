import importlib
import logging
import math
import random
import string
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta
from functools import cached_property, partial

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange as ObjectChange_
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connection, connections, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.operations.special import RunSQL, SeparateDatabaseAndState
from django.db.models.signals import post_save, pre_delete
from django.db.utils import ProgrammingError
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

from netbox_branching.choices import BranchEventTypeChoices, BranchMergeStrategyChoices, BranchStatusChoices
from netbox_branching.constants import BRANCH_ACTIONS, SKIP_INDEXES
from netbox_branching.contextvars import active_branch
from netbox_branching.merge_strategies import get_merge_strategy
from netbox_branching.signals import *
from netbox_branching.utilities import (
    BranchActionIndicator,
    ChangeSummary,
    activate_branch,
    get_branchable_object_types,
    get_sql_results,
    get_tables_to_replicate,
    record_applied_change,
    supports_branching,
)

from .changes import ObjectChange

__all__ = (
    'Branch',
    'BranchEvent',
)


# pg_catalog.set_config(name, value, is_local=true) is the function-call form
# of SET LOCAL — value is passed as a query parameter rather than interpolated.
_SET_SEARCH_PATH = "SELECT pg_catalog.set_config('search_path', %s, true)"


@contextmanager
def _branch_isolated_runsql(branch_schema, main_schema):
    """
    Restrict ``search_path`` to ``branch_schema`` for each ``RunSQL`` body, then
    restore ``<branch>,<main>`` afterwards. Other operation types keep the
    default search_path because they may need cross-schema visibility (e.g. FKs
    to ``auth.User`` / ``contenttypes``, which aren't replicated to branches).

    Implemented by monkey-patching ``RunSQL.database_forwards`` for the
    duration of the block. Safe because NetBox runs branch migrations as RQ
    jobs (one per worker process); concurrent ``Branch.migrate()`` calls in
    the same process would race.
    """
    original = RunSQL.database_forwards

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        connection = schema_editor.connection
        with connection.cursor() as cursor:
            cursor.execute(_SET_SEARCH_PATH, [branch_schema])
        try:
            return original(self, app_label, schema_editor, from_state, to_state)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(_SET_SEARCH_PATH, [f'{branch_schema},{main_schema}'])

    RunSQL.database_forwards = database_forwards
    try:
        yield
    finally:
        RunSQL.database_forwards = original


def _fake_for_branch(migration):
    """
    Return True if a migration should be faked when applied to a branch schema, False otherwise.

    Decision order:
    1. If the migration module sets a ``fake_on_branch`` attribute, that value is respected
       directly: ``True`` forces faking, ``False`` forces the migration to run.
    2. Otherwise, fall back to a heuristic: fake migrations whose model-specific operations
       affect only non-branchable models. This prevents RunSQL operations from inadvertently
       acting on the main (public) schema via the search_path.

    Migrations with no model-specific operations (e.g. pure RunSQL or RunPython) are not faked
    by the heuristic, as we cannot determine their intent without executing them. Authors of
    such migrations should set ``fake_on_branch`` explicitly when needed.

    SeparateDatabaseAndState operations are not supported and will be skipped with an error.
    """
    logger = logging.getLogger('netbox_branching.branch.migrate')

    # Check for an explicit per-migration override
    try:
        module = importlib.import_module(f'{migration.app_label}.migrations.{migration.name}')
    except ModuleNotFoundError:
        module = None
    if module is not None and (explicit := getattr(module, 'fake_on_branch', None)) is not None:
        return bool(explicit)

    has_model_operations = False
    for operation in migration.operations:
        if isinstance(operation, SeparateDatabaseAndState):
            logger.error(
                f"Migration {migration} contains SeparateDatabaseAndState, which is not supported "
                f"for branch schema migration. This migration will not be faked."
            )
            return False
        if (model_name := getattr(operation, 'model_name', None)) is None:
            continue
        has_model_operations = True
        # If any operation targets a branchable model, don't fake this migration
        try:
            model = apps.get_model(migration.app_label, model_name)
        except LookupError:
            # If we can't resolve the model (e.g. removed in a squashed migration),
            # conservatively treat it as branchable and don't fake.
            logger.warning(f"Could not resolve model {migration.app_label}.{model_name}; not faking {migration}")
            return False
        if supports_branching(model):
            return False
    return has_model_operations


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
    merge_strategy = models.CharField(
        verbose_name=_('merge strategy'),
        max_length=50,
        choices=BranchMergeStrategyChoices,
        blank=True,
        null=True,
        default=None,
        help_text=_('Strategy used to merge this branch')
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
    # Branch actions
    #

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

        # Generate a request ID for correlating ObjectChange records from this sync
        request_id = uuid.uuid4()

        try:
            with activate_branch(self), transaction.atomic(using=self.connection_name):
                models = set()
                branchable_models = {ct.model_class() for ct in get_branchable_object_types()}

                # Apply each change from the main schema
                for change in changes:
                    model_class = change.changed_object_type.model_class()
                    models.add(model_class)
                    if change.action == ObjectChangeActionChoices.ACTION_DELETE:
                        cascade_models = self._handle_sync_delete(
                            change, branchable_models, user, logger, request_id=request_id
                        )
                        models.update(cascade_models)
                    else:
                        change.apply(self, using=self.connection_name, logger=logger, skip_missing=True)

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
                if fake:
                    logger.debug(f"Faking migration {migration} (no branchable models affected)")
                else:
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
        main_schema = get_plugin_config('netbox_branching', 'main_schema')
        if plan := executor.migration_plan(targets):
            try:
                # Activate the branch so that any ORM queries inside data migrations
                # (RunPython) route to the branch schema rather than main. Without this,
                # historical-model queries fall through the BranchAwareRouter to the default
                # connection and read from main, which may have already been migrated past
                # columns the branch's pending migration still depends on.
                with activate_branch(self), _branch_isolated_runsql(self.schema_name, main_schema):
                    # Apply each migration individually, faking those that only affect
                    # non-branchable models to prevent RunSQL from inadvertently operating
                    # on the main schema via the search_path. See GitHub issue #423.
                    full_plan = executor.migration_plan(executor.loader.graph.leaf_nodes(), clean_start=True)
                    migrations_to_run = {m for m, _ in plan}
                    # _create_project_state is a private Django API (MigrationExecutor). It builds
                    # the current ProjectState from all applied migrations, which apply_migration
                    # requires as its starting point. There is no public equivalent as of Django 5.x.
                    state = executor._create_project_state(with_applied_migrations=True)
                    for migration, _ in full_plan:
                        if not migrations_to_run:
                            break
                        if migration in migrations_to_run:
                            fake = _fake_for_branch(migration)
                            state = executor.apply_migration(state, migration, fake=fake)
                            migrations_to_run.remove(migration)
            except Exception as e:
                if err_message := str(e):
                    logger.error(err_message)
                # Mark the branch as failed so it cannot be activated in a partially-migrated
                # state. Persist any migrations already recorded as applied.
                Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.FAILED)
                self.status = BranchStatusChoices.FAILED
                raise
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
                    raise

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
                            except Exception:
                                logger.error(sql)
                                raise
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
