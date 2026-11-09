import logging
from abc import ABC, abstractmethod

from django.contrib.contenttypes.models import ContentType
from django.db import DEFAULT_DB_ALIAS
from mptt.models import MPTTModel

from core.choices import ObjectChangeActionChoices

__all__ = (
    'MergeStrategy',
    'get_merge_strategy',
    'get_module_move_derived_fields',
)

# Fields which a Module move (dcim.ModuleMovePlan) rewrites on the objects it relocates
MODULE_MOVE_DERIVED_FIELDS = (
    'device',
    'name',
    'label',
    'parent',
    'position',
    '_site',
    '_location',
    '_rack',
    'last_updated',
)


def _field_changed(change, field):
    return (change.prechange_data or {}).get(field) != (change.postchange_data or {}).get(field)


def get_module_move_derived_fields(changes):
    """
    Map the PK of each UPDATE in changes which a Module move made as a side effect (to a nested Module, ModuleBay,
    component or PortMapping) to the fields the move rewrote. Undoing those fields on their own fails validation;
    undoing the Module's own change moves everything back together. (#722)
    """
    module_type = ContentType.objects.get_by_natural_key('dcim', 'module')
    updates = changes.filter(action=ObjectChangeActionChoices.ACTION_UPDATE)

    # A Module whose bay changed is the root of a move; its request also holds the move's side effects
    request_ids = {
        change.request_id
        for change in updates.filter(changed_object_type=module_type)
        if _field_changed(change, 'module_bay')
    }
    if not request_ids:
        return {}
    changes_by_request = {}
    for change in updates.filter(request_id__in=request_ids):
        changes_by_request.setdefault(change.request_id, []).append(change)

    frontport_type = ContentType.objects.get_by_natural_key('dcim', 'frontport')
    portmapping_type = ContentType.objects.get_by_natural_key('dcim', 'portmapping')
    derived = {}
    for request_changes in changes_by_request.values():
        module_changes = [c for c in request_changes if c.changed_object_type_id == module_type.pk]
        moved_module_ids = {c.changed_object_id for c in module_changes}
        for change in module_changes:
            # A nested Module keeps its bay and only follows the root to the new device
            if not _field_changed(change, 'module_bay'):
                derived[change.pk] = MODULE_MOVE_DERIVED_FIELDS
        moved_front_port_ids = set()
        for change in request_changes:
            if (change.postchange_data or {}).get('module') in moved_module_ids:
                derived[change.pk] = MODULE_MOVE_DERIVED_FIELDS
                if change.changed_object_type_id == frontport_type.pk:
                    moved_front_port_ids.add(change.changed_object_id)
        for change in request_changes:
            if (
                change.changed_object_type_id == portmapping_type.pk
                and (change.postchange_data or {}).get('front_port') in moved_front_port_ids
            ):
                derived[change.pk] = MODULE_MOVE_DERIVED_FIELDS

    return derived


class MergeStrategy(ABC):
    """
    Abstract base class for merge strategies.
    """

    # Ordering for changes queryset in revert() operation
    # Merge always uses chronological order ('time')
    revert_changes_ordering = '-time'  # Reverse chronological order (newest first)

    @abstractmethod
    def merge(self, branch, changes, request, logger, user):
        """
        Merge changes from the branch into the main schema.

        Args:
            branch: The Branch instance being merged
            changes: QuerySet of ObjectChanges to merge
            request: Django request object for event tracking
            logger: Logger instance for logging
            user: User who initiated the merge
        """

    @abstractmethod
    def revert(self, branch, changes, request, logger, user):
        """
        Revert changes that were previously merged.

        Args:
            branch: The Branch instance being reverted
            changes: QuerySet of ObjectChanges to revert
            request: Django request object for event tracking
            logger: Logger instance for logging
            user: User who initiated the revert
        """

    def _update_dependent_objects(self, pks_by_model, logger):
        """
        Update the objects which depend on those just applied (e.g. the CablePaths traversing a Cable).
        Applying a create writes the object with a raw save, which bypasses Model.save() and the dependent
        objects it maintains, and an update can land before the related changes it depends on (e.g. a
        re-terminated Cable before its CableTerminations); models expose the work through
        update_dependent_objects(). (#469)

        Must be called only once every change has been applied: retracing a Cable, for instance, requires
        its CableTerminations to exist.

        Args:
            pks_by_model: Mapping of model classes to the PKs of the objects applied for each
            logger: Logger instance for logging
        """
        for model, pks in pks_by_model.items():
            if not hasattr(model, 'update_dependent_objects'):
                continue
            queryset = model.objects.using(DEFAULT_DB_ALIAS).filter(pk__in=pks)
            for instance in queryset.iterator(chunk_size=100):
                logger.debug(f'Updating objects dependent on {model._meta.verbose_name} {instance.pk}')
                instance.update_dependent_objects()

    def _clean(self, models):
        """
        Called after syncing, merging, or reverting a branch.
        """
        logger = logging.getLogger('netbox_branching.branch')

        for model in models:
            # Recalculate MPTT as needed
            if issubclass(model, MPTTModel):
                logger.debug(f'Recalculating MPTT for model {model}')
                model.objects.rebuild()


def get_merge_strategy(strategy_name):
    """
    Get the appropriate merge strategy class based on the strategy name.

    Args:
        strategy_name: String name of the strategy from BranchMergeStrategyChoices, or None

    Returns:
        MergeStrategy class (caller should instantiate)

    Raises:
        ValueError: If the strategy name is unknown
    """
    from netbox_branching.choices import BranchMergeStrategyChoices

    from .iterative import IterativeMergeStrategy
    from .squash import SquashMergeStrategy

    # Default to ITERATIVE if strategy_name is None
    if strategy_name is None:
        strategy_name = BranchMergeStrategyChoices.ITERATIVE

    strategies = {
        BranchMergeStrategyChoices.SQUASH: SquashMergeStrategy,
        BranchMergeStrategyChoices.ITERATIVE: IterativeMergeStrategy,
    }

    try:
        return strategies[strategy_name]
    except KeyError as exc:
        raise ValueError(f'Invalid strategy name: {strategy_name}') from exc
