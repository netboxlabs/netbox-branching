"""
Functions for collapsing and ordering ObjectChanges during branch merge operations.
"""
import logging

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import DEFAULT_DB_ALIAS, models

from core.choices import ObjectChangeActionChoices
from netbox_branching.utilities import update_object
from utilities.data import shallow_compare_dict
from utilities.serialization import deserialize_object


__all__ = (
    'CollapsedChange',
    'order_collapsed_changes',
)


class CollapsedChange:
    """
    Represents a collapsed set of ObjectChanges for a single object.
    """
    def __init__(self, key, model_class):
        self.key = key  # (content_type_id, object_id)
        self.model_class = model_class
        self.changes = []  # List of ObjectChange instances, ordered by time
        self.final_action = None  # 'create', 'update', 'delete', or 'skip'
        self.prechange_data = None
        self.postchange_data = None
        self.last_change = None  # The most recent ObjectChange (for metadata)

        # Dependencies for ordering
        self.depends_on = set()  # Set of keys this change depends on
        self.depended_by = set()  # Set of keys that depend on this change

    def __repr__(self):
        obj_id = self.key[1]
        suffix = f" ({self.key[2]})" if len(self.key) > 2 else ""
        return (
            f"<CollapsedChange {self.model_class.__name__}:{obj_id}{suffix} "
            f"action={self.final_action} changes={len(self.changes)}>"
        )

    def collapse(self, logger):
        """
        Collapse the list of ObjectChanges for this object into a single action.

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
        if not self.changes:
            self.final_action = None
            self.prechange_data = None
            self.postchange_data = None
            self.last_change = None
            return

        # Sort by time (oldest first)
        self.changes = sorted(self.changes, key=lambda c: c.time)

        # Check if there's a DELETE anywhere in the changes
        has_delete = any(c.action == 'delete' for c in self.changes)
        has_create = any(c.action == 'create' for c in self.changes)

        logger.debug(f"  Collapsing {len(self.changes)} changes...")

        if has_delete:
            if has_create:
                # CREATE + DELETE = skip entirely
                logger.debug("  -> Action: SKIP (created and deleted in branch)")
                self.final_action = 'skip'
                self.prechange_data = None
                self.postchange_data = None
                self.last_change = self.changes[-1]
                return
            else:
                # Just DELETE (ignore all other changes like updates)
                # prechange_data: original state from first change
                # postchange_data: postchange_data from DELETE ObjectChange
                logger.debug(f"  -> Action: DELETE (keeping only DELETE, ignoring {len(self.changes) - 1} other changes)")
                delete_change = next(c for c in self.changes if c.action == 'delete')
                self.final_action = 'delete'
                self.prechange_data = self.changes[0].prechange_data
                self.postchange_data = delete_change.postchange_data  # Should be None for DELETE, but use actual value
                self.last_change = delete_change
                return

        # No DELETE - handle CREATE or UPDATEs
        first_action = self.changes[0].action
        first_change = self.changes[0]
        last_change = self.changes[-1]

        # Created (with possible updates) -> single create
        if first_action == 'create':
            # prechange_data: from first ObjectChange (should be None for CREATE)
            # postchange_data: merged from all changes
            self.prechange_data = first_change.prechange_data
            self.postchange_data = {}
            for change in self.changes:
                # Merge postchange_data, later changes overwrite earlier ones
                if change.postchange_data:
                    self.postchange_data.update(change.postchange_data)
            logger.debug(f"  -> Action: CREATE (collapsed {len(self.changes)} changes)")
            self.final_action = 'create'
            self.last_change = last_change
            return

        # Only updates -> single update
        # prechange_data: original state from first change
        # postchange_data: final state after all updates
        self.prechange_data = first_change.prechange_data
        self.postchange_data = {}

        if self.prechange_data:
            self.postchange_data.update(self.prechange_data)

        for change in self.changes:
            if change.postchange_data:
                self.postchange_data.update(change.postchange_data)

        logger.debug(f"  -> Action: UPDATE (collapsed {len(self.changes)} changes)")
        self.final_action = 'update'
        self.last_change = last_change

    def apply(self, branch, using=DEFAULT_DB_ALIAS, logger=None):
        """
        Apply this collapsed change to the database.
        Similar to ObjectChange.apply() but works with collapsed data.
        """
        logger = logger or logging.getLogger('netbox_branching.collapse_merge.apply')
        model = self.model_class
        object_id = self.key[1]

        # Run data migrators on the last change (to apply any necessary migrations)
        self.last_change.migrate(branch)

        # Creating a new object
        if self.final_action == 'create':
            logger.debug(f'  Creating {model._meta.verbose_name} {object_id}')

            if hasattr(model, 'deserialize_object'):
                instance = model.deserialize_object(self.postchange_data, pk=object_id)
            else:
                instance = deserialize_object(model, self.postchange_data, pk=object_id)

            try:
                instance.object.full_clean()
            except (FileNotFoundError) as e:
                # If a file was deleted later in this branch it will fail here
                # so we need to ignore it. We can assume the NetBox state is valid.
                logger.warning(f'  Ignoring missing file: {e}')
            instance.save(using=using)

        # Modifying an object
        elif self.final_action == 'update':
            logger.debug(f'  Updating {model._meta.verbose_name} {object_id}')

            try:
                instance = model.objects.using(using).get(pk=object_id)
            except model.DoesNotExist:
                logger.error(f'  {model._meta.verbose_name} {object_id} not found for update')
                raise

            # Calculate what fields changed from the collapsed changes
            # We need to figure out what changed between initial and final state
            initial_data = self.prechange_data or {}
            final_data = self.postchange_data or {}

            # Only update fields that actually changed
            changed_fields = {}
            for key, final_value in final_data.items():
                initial_value = initial_data.get(key)
                if initial_value != final_value:
                    changed_fields[key] = final_value

            logger.debug(f'    Updating {len(changed_fields)} fields: {list(changed_fields.keys())}')
            update_object(instance, changed_fields, using=using)

        # Deleting an object
        elif self.final_action == 'delete':
            logger.debug(f'  Deleting {model._meta.verbose_name} {object_id}')

            try:
                instance = model.objects.using(using).get(pk=object_id)
                instance.delete(using=using)
            except model.DoesNotExist:
                logger.debug(f'  {model._meta.verbose_name} {object_id} already deleted; skipping')

    def undo(self, branch, using=DEFAULT_DB_ALIAS, logger=None):
        """
        Undo this collapsed change from the database (reverse of apply).
        Follows the same pattern as ObjectChange.undo().
        """
        logger = logger or logging.getLogger('netbox_branching.collapse_merge.undo')
        model = self.model_class
        object_id = self.key[1]

        # Run data migrators on the last change (in revert mode)
        self.last_change.migrate(branch, revert=True)

        # Undoing a CREATE: delete the object
        if self.final_action == 'create':
            logger.debug(f'  Undoing creation of {model._meta.verbose_name} {object_id}')
            try:
                instance = model.objects.using(using).get(pk=object_id)
                instance.delete(using=using)
            except model.DoesNotExist:
                logger.debug(f'  {model._meta.verbose_name} {object_id} does not exist; skipping')

        # Undoing an UPDATE: revert to the original state
        elif self.final_action == 'update':
            logger.debug(f'  Undoing update of {model._meta.verbose_name} {object_id}')

            try:
                instance = model.objects.using(using).get(pk=object_id)
                # Compute diff and apply 'pre' values (like ObjectChange.undo() does)
                diff = _diff_object_change_data(
                    ObjectChangeActionChoices.ACTION_UPDATE,
                    self.prechange_data,
                    self.postchange_data
                )
                update_object(instance, diff['pre'], using=using)
            except model.DoesNotExist:
                logger.debug(f'  {model._meta.verbose_name} {object_id} does not exist; skipping')

        # Undoing a DELETE: restore the object
        elif self.final_action == 'delete':
            logger.debug(f'  Undoing deletion (restoring) {model._meta.verbose_name} {object_id}')

            prechange_data = self.prechange_data or {}

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
    for update in updates:
        # Check if UPDATE references deleted object in prechange_data
        # This means the UPDATE had a reference that it's removing
        # The UPDATE must happen BEFORE the DELETE so the FK reference is removed first
        if update.changes[0].prechange_data:
            prechange_refs = _get_fk_references(
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
            postchange_refs = _get_fk_references(
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
    for create in creates:
        if create.postchange_data:
            # Check if this CREATE references other created objects
            refs = _get_fk_references(
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
    for delete in deletes:
        if delete.prechange_data:
            # Check if this DELETE references other deleted objects
            refs = _get_fk_references(
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


def _has_fk_to(collapsed, target_model_class, target_obj_id):
    """
    Check if a CollapsedChange has a foreign key reference to a specific object.
    Returns True if any FK field in postchange_data points to the target object.
    """
    if not collapsed.postchange_data:
        return False

    for field in collapsed.model_class._meta.get_fields():
        if isinstance(field, models.ForeignKey):
            fk_value = collapsed.postchange_data.get(field.name)
            if fk_value:
                # Check if this FK points to target model
                related_model = field.related_model
                if related_model == target_model_class and fk_value == target_obj_id:
                    return True
    return False


def _split_bidirectional_cycles(collapsed_changes, logger):
    """
    Special case: Preemptively detect and split CREATE operations involved in
    bidirectional FK cycles.

    For each CREATE A with a nullable FK to CREATE B, check if CREATE B has any FK back to A.
    If so, split CREATE A by setting the nullable FK to NULL and creating a separate UPDATE.

    This handles the common case of bidirectional FK relationships (e.g., Circuit ↔ CircuitTermination)
    without needing complex cycle detection.
    """

    creates = {key: c for key, c in collapsed_changes.items() if c.final_action == 'create'}
    splits_made = 0

    for key_a, create_a in list(creates.items()):
        if not create_a.postchange_data:
            continue

        # Find nullable FK fields in this CREATE
        for field in create_a.model_class._meta.get_fields():
            if not (isinstance(field, models.ForeignKey) and field.null):
                continue

            fk_value = create_a.postchange_data.get(field.name)
            if not fk_value:
                continue

            # Get the target's key
            related_model = field.related_model
            target_ct = ContentType.objects.get_for_model(related_model)
            key_b = (target_ct.id, fk_value)

            # Is target also being created?
            if key_b not in creates:
                continue

            create_b = creates[key_b]

            # Does target have FK back to us? (bidirectional cycle)
            if _has_fk_to(create_b, create_a.model_class, key_a[1]):
                logger.info(
                    f"  Detected bidirectional cycle: {create_a.model_class.__name__}:{key_a[1]} ↔ "
                    f"{create_b.model_class.__name__}:{key_b[1]} (via {field.name})"
                )

                # Split create_a: set nullable FK to NULL, create UPDATE to set it later
                original_postchange = dict(create_a.postchange_data)
                create_a.postchange_data[field.name] = None

                # Create UPDATE operation
                update_key = (key_a[0], key_a[1], f'update_{field.name}')
                update_collapsed = CollapsedChange(update_key, create_a.model_class)
                update_collapsed.changes = [create_a.last_change]
                update_collapsed.final_action = 'update'
                update_collapsed.prechange_data = dict(create_a.postchange_data)
                update_collapsed.postchange_data = original_postchange
                update_collapsed.last_change = create_a.last_change

                # Add UPDATE to collapsed_changes
                collapsed_changes[update_key] = update_collapsed
                splits_made += 1

                break


def _log_cycle_details(remaining, collapsed_changes, logger, max_to_show=5):
    """
    Log details about nodes involved in a dependency cycle.
    Used for debugging when cycles are detected.
    """
    for key, deps in list(remaining.items())[:max_to_show]:
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

    if len(remaining) > max_to_show:
        logger.error(f"    ... and {len(remaining) - max_to_show} more nodes in cycle")


def _dependency_order_by_references(collapsed_changes, logger):
    """
    Orders collapsed changes using topological sort with cycle detection.

    Uses Kahn's algorithm to order nodes respecting their dependency graph.
    Reference: https://en.wikipedia.org/wiki/Topological_sorting#Kahn's_algorithm

    The algorithm processes nodes in "layers" - first all nodes with no dependencies,
    then all nodes whose dependencies have been satisfied, and so on.

    When multiple nodes have no dependencies (equal priority in the dependency graph),
    they are ordered by action type priority: DELETE (0) -> UPDATE (1) -> CREATE (2).

    If cycles are detected, raises an exception. Bidirectional cycles should be handled
    by _split_bidirectional_cycles() before calling this method.

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

            # Log details about the nodes involved in the cycle (for debugging)
            _log_cycle_details(remaining, collapsed_changes, logger)

            raise Exception(
                f"Cycle detected in dependency graph. {len(remaining)} changes are involved in "
                f"circular dependencies and cannot be ordered. This may indicate a complex cycle "
                f"that could not be automatically resolved. Check the logs above for details."
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

        # Log details about the remaining unprocessed nodes (for debugging)
        _log_cycle_details(remaining, collapsed_changes, logger)

        raise Exception(
            f"Ordering by references exceeded maximum iterations ({max_iterations}). "
            f"{len(remaining)} changes could not be ordered, possibly due to a complex cycle. "
            f"Check the logs above for details."
        )

    logger.info(f"  Ordering by references completed: {len(ordered)} changes ordered")
    return ordered


def order_collapsed_changes(collapsed_changes, logger):
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

    # Reset dependencies
    for collapsed in to_process.values():
        collapsed.depends_on = set()
        collapsed.depended_by = set()

    # Preemptively detect and split bidirectional FK cycles
    # This may add new UPDATE operations to to_process
    _split_bidirectional_cycles(to_process, logger)

    # Group by action and sort each group by time - need this to build the
    # dependency graph correctly
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

    _build_fk_dependency_graph(deletes, updates, creates, logger)
    ordered_keys = _dependency_order_by_references(to_process, logger)

    # Convert keys back to collapsed changes
    result = [to_process[key] for key in ordered_keys]
    return result
