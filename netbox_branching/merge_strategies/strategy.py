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
)


def _field_changed(change, field):
    return (change.prechange_data or {}).get(field) != (change.postchange_data or {}).get(field)


def _get_move_writes(root, later_changes, module_type, frontport_type, portmapping_type):
    """
    Return the changes (ordered, same request) which the move of root wrote as side effects: the unbroken run of
    first writes to its nested Modules, bays, components and PortMappings directly after root.
    """
    old_device = (root.prechange_data or {}).get('device')
    new_device = (root.postchange_data or {}).get('device')
    moved_module_ids = {root.changed_object_id}
    moved_front_port_ids = set()
    writes, seen = [], set()
    for change in later_changes:
        pre, post = change.prechange_data or {}, change.postchange_data or {}
        key = (change.changed_object_type_id, change.changed_object_id)
        # The planner writes each object once and, on a cross-device move, always moves it to the new device
        if key in seen or (pre.get('device'), post.get('device')) != (old_device, new_device):
            break
        if change.changed_object_type_id == module_type.pk:
            # A nested Module keeps its bay and only follows the root to the new device
            if old_device == new_device or pre.get('module_bay') != post.get('module_bay'):
                break
            moved_module_ids.add(change.changed_object_id)
        elif change.changed_object_type_id == portmapping_type.pk:
            if post.get('front_port') not in moved_front_port_ids:
                break
        elif post.get('module') in moved_module_ids:
            if change.changed_object_type_id == frontport_type.pk:
                moved_front_port_ids.add(change.changed_object_id)
        else:
            break
        seen.add(key)
        writes.append(change)
    return writes


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
    for change in updates.filter(request_id__in=request_ids).order_by('pk'):
        changes_by_request.setdefault(change.request_id, []).append(change)

    frontport_type = ContentType.objects.get_by_natural_key('dcim', 'frontport')
    portmapping_type = ContentType.objects.get_by_natural_key('dcim', 'portmapping')
    derived = {}
    for request_changes in changes_by_request.values():
        root_indexes = [
            i for i, change in enumerate(request_changes)
            if change.changed_object_type_id == module_type.pk and _field_changed(change, 'module_bay')
        ]
        # Each move's side effects follow its root and precede the next move in the request
        for i, start in enumerate(root_indexes):
            stop = root_indexes[i + 1] if i + 1 < len(root_indexes) else len(request_changes)
            writes = _get_move_writes(
                request_changes[start], request_changes[start + 1:stop], module_type, frontport_type, portmapping_type
            )
            for change in writes:
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
