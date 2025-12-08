"""
Squash merge strategy implementation with functions for collapsing and ordering ObjectChanges.
"""
from enum import Enum

from django.contrib.contenttypes.models import ContentType
from django.db import DEFAULT_DB_ALIAS, models

from netbox.context_managers import event_tracking

from .strategy import MergeStrategy


__all__ = (
    'SquashMergeStrategy',
)


class ActionType(str, Enum):
    """
    Enum for collapsed change action types.
    """
    CREATE = 'create'
    UPDATE = 'update'
    DELETE = 'delete'
    SKIP = 'skip'


class CollapsedChange:
    """
    Represents a collapsed set of ObjectChanges for a single object.
    """
    def __init__(self, key, model_class):
        self.key = key  # (content_type_id, object_id)
        self.model_class = model_class
        self.change_count = 0  # Number of changes processed
        self.final_action = None  # ActionType enum value or None
        self.prechange_data = {}
        self.postchange_data = {}
        self.last_change = None  # The most recent ObjectChange (for metadata)

        # Dependencies for ordering
        self.depends_on = set()  # Set of keys this change depends on
        self.depended_by = set()  # Set of keys that depend on this change

    def __repr__(self):
        obj_id = self.key[1]
        suffix = f" ({self.key[2]})" if len(self.key) > 2 else ""
        return (
            f"<CollapsedChange {self.model_class.__name__}:{obj_id}{suffix} "
            f"action={self.final_action} changes={self.change_count}>"
        )

    def add_change(self, change, logger):
        """
        Incrementally process a single ObjectChange and update the collapsed state.
        This processes changes one-by-one to avoid storing all ObjectChanges in memory.

        Assumes changes are added in chronological order.
        """
        self.change_count += 1
        self.last_change = change

        # Dispatch to specific handler based on action type
        if change.action == 'create':
            self._add_change_create(change, logger)
        elif change.action == 'update':
            self._add_change_update(change, logger)
        elif change.action == 'delete':
            self._add_change_delete(change, logger)

    def _add_change_create(self, change, logger):
        """Handle CREATE action."""
        if self.final_action is None:
            logger.debug(f"  [{self.change_count}] CREATE")
            self.final_action = ActionType.CREATE
            self._set_initial_data(change)
        else:
            logger.warning(f"  [{self.change_count}] Unexpected CREATE after {self.final_action} for {self}")

    def _add_change_update(self, change, logger):
        """Handle UPDATE action."""
        if self.final_action is None:
            self.final_action = ActionType.UPDATE
            self._set_initial_data(change)
        elif self.final_action in (ActionType.CREATE, ActionType.UPDATE):
            logger.debug(f"  [{self.change_count}] UPDATE after {self.final_action} (still {self.final_action})")
            if change.postchange_data:
                self.postchange_data.update(change.postchange_data)
        else:
            logger.warning(f"  [{self.change_count}] Unexpected UPDATE after {self.final_action} for {self}")

    def _add_change_delete(self, change, logger):
        """Handle DELETE action."""
        if self.final_action is None:
            self.final_action = ActionType.DELETE
            self._set_initial_data(change)
        elif self.final_action == ActionType.CREATE:
            # CREATE + DELETE = SKIP
            logger.debug(f"  [{self.change_count}] DELETE after CREATE -> SKIP")
            self.final_action = ActionType.SKIP
            self.prechange_data = {}
            self.postchange_data = {}
        elif self.final_action == ActionType.UPDATE:
            # UPDATE + DELETE = DELETE
            self.final_action = ActionType.DELETE
            self.postchange_data = change.postchange_data
        else:
            # DELETE after DELETE or SKIP is unexpected
            logger.warning(f"  [{self.change_count}] Unexpected DELETE after {self.final_action} for {self}")

    def _set_initial_data(self, change):
        """Helper to set initial pre/postchange data for first CREATE or UPDATE."""
        self.prechange_data = change.prechange_data or {}
        if change.postchange_data:
            self.postchange_data.update(change.postchange_data)

    def generate_object_change(self):
        """
        Generate a dummy ObjectChange instance from this collapsed change.
        Used to leverage the standard ObjectChange.apply() and undo() methods.
        """
        from netbox_branching.models import ObjectChange
        app_label, model = self.key[0].split('.')
        dummy_change = ObjectChange(
            action=self.final_action.value if self.final_action else None,
            changed_object_type=ContentType.objects.get_by_natural_key(app_label, model),
            changed_object_id=self.key[1],
            prechange_data=self.prechange_data,
            postchange_data=self.postchange_data,
        )
        # Use last_change for migrate() to have the correct metadata
        dummy_change.pk = self.last_change.pk
        return dummy_change


class SquashMergeStrategy(MergeStrategy):
    """
    Squash merge strategy that collapses multiple changes per object into a single operation.
    """
    # Override: squash strategy needs chronological order for both merge and revert
    # because the collapse logic expects CREATE -> UPDATE -> DELETE order
    revert_changes_ordering = 'time'

    def merge(self, branch, changes, request, logger, user):
        """
        Apply changes after collapsing them by object and ordering by dependencies.
        """
        models = set()

        logger.info("Collapsing ObjectChanges by object (incremental)...")
        collapsed_changes = {}

        for change in changes:
            app_label, model = change.changed_object_type.natural_key()
            model_label = f"{app_label}.{model}"
            key = (model_label, change.changed_object_id)

            if key not in collapsed_changes:
                model_class = change.changed_object_type.model_class()
                collapsed = CollapsedChange(key, model_class)
                collapsed_changes[key] = collapsed
                logger.debug(f"New object: {model_class.__name__}:{change.changed_object_id}")

            # Incrementally process each change to avoid storing all in memory
            collapsed_changes[key].add_change(change, logger)

        # Order collapsed changes based on dependencies
        ordered_changes = SquashMergeStrategy._order_collapsed_changes(collapsed_changes, logger)

        # Apply collapsed changes in order
        logger.info(f"Applying {len(ordered_changes)} collapsed changes...")
        for i, collapsed in enumerate(ordered_changes, 1):
            model_class = collapsed.model_class
            models.add(model_class)

            last_change = collapsed.last_change

            logger.info(f"  [{i}/{len(ordered_changes)}] {collapsed.final_action.upper()} "
                       f"{model_class.__name__}:{collapsed.key[1]} "
                       f"(from {collapsed.change_count} original changes)")

            with event_tracking(request):
                request.id = last_change.request_id
                request.user = user

                # Create a dummy ObjectChange from the collapsed change and apply it
                dummy_change = collapsed.generate_object_change()
                dummy_change.apply(branch, using=DEFAULT_DB_ALIAS, logger=logger)

        # Perform cleanup tasks
        self._clean(models)

    def revert(self, branch, changes, request, logger, user):
        """
        Undo changes after collapsing them by object and ordering by dependencies.
        """
        models = set()

        # Group changes by object and create CollapsedChange objects
        logger.info("Collapsing ObjectChanges by object (incremental)...")
        collapsed_changes = {}
        change_count = 0

        for change in changes:
            change_count += 1
            app_label, model = change.changed_object_type.natural_key()
            model_label = f"{app_label}.{model}"
            key = (model_label, change.changed_object_id)

            if key not in collapsed_changes:
                model_class = change.changed_object_type.model_class()
                collapsed = CollapsedChange(key, model_class)
                collapsed_changes[key] = collapsed
                logger.debug(f"New object: {model_class.__name__}:{change.changed_object_id}")

            # Incrementally process each change to avoid storing all in memory
            collapsed_changes[key].add_change(change, logger)

        logger.info(f"  {change_count} changes collapsed into {len(collapsed_changes)} objects")

        # Order collapsed changes for revert (reverse of merge order)
        merge_order = SquashMergeStrategy._order_collapsed_changes(collapsed_changes, logger)
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
                request.user = user

                # Create a dummy ObjectChange from the collapsed change and undo it
                dummy_change = collapsed.generate_object_change()
                dummy_change.undo(branch, using=DEFAULT_DB_ALIAS, logger=logger)

        # Perform cleanup tasks
        self._clean(models)

    @staticmethod
    def _get_fk_references(model_class, data, changed_objects):
        """
        Find foreign key references in the given data that point to objects in changed_objects.
        Returns a set of (model_label, object_id) tuples where model_label is "app.model".
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
                    app_label, model = related_ct.natural_key()
                    model_label = f"{app_label}.{model}"
                    ref_key = (model_label, fk_value)

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
        for update in updates:
            # Check if UPDATE references deleted object in prechange_data
            # This means the UPDATE had a reference that it's removing
            # The UPDATE must happen BEFORE the DELETE so the FK reference is removed first
            if update.prechange_data:
                prechange_refs = SquashMergeStrategy._get_fk_references(
                    update.model_class,
                    update.prechange_data,
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
                postchange_refs = SquashMergeStrategy._get_fk_references(
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
                refs = SquashMergeStrategy._get_fk_references(
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
                refs = SquashMergeStrategy._get_fk_references(
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

    @staticmethod
    def _split_bidirectional_cycles(collapsed_changes, logger):
        """
        Special case: Preemptively detect and split CREATE operations involved in
        bidirectional FK cycles.

        For each CREATE A with a nullable FK to CREATE B, check if CREATE B has any FK back to A.
        If so, split CREATE A by setting the nullable FK to NULL and creating a separate UPDATE.

        This handles the common case of bidirectional FK relationships (e.g., Circuit ↔ CircuitTermination)
        without needing complex cycle detection.
        """

        creates = {key: c for key, c in collapsed_changes.items() if c.final_action == ActionType.CREATE}
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
                app_label, model = target_ct.natural_key()
                model_label = f"{app_label}.{model}"
                key_b = (model_label, fk_value)

                # Is target also being created?
                if key_b not in creates:
                    continue

                create_b = creates[key_b]

                # Does target have FK back to us? (bidirectional cycle)
                if SquashMergeStrategy._has_fk_to(create_b, create_a.model_class, key_a[1]):
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
                    update_collapsed.change_count = 1  # Synthetic update from split
                    update_collapsed.final_action = ActionType.UPDATE
                    update_collapsed.prechange_data = dict(create_a.postchange_data)
                    update_collapsed.postchange_data = original_postchange
                    update_collapsed.last_change = create_a.last_change

                    # Add UPDATE to collapsed_changes
                    collapsed_changes[update_key] = update_collapsed
                    splits_made += 1

                    break

    @staticmethod
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

        If cycles are detected, raises an exception.

        Returns: ordered list of keys
        """
        logger.info("Adjusting ordering by references...")

        # Define action priority (lower number = higher priority = processed first)
        action_priority = {
            ActionType.DELETE: 0,  # DELETEs should happen first
            ActionType.UPDATE: 1,  # UPDATEs in the middle
            ActionType.CREATE: 2,  # CREATEs should happen last
            ActionType.SKIP: 3,    # SKIPs should never be in the sort, but just in case
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
                SquashMergeStrategy._log_cycle_details(remaining, collapsed_changes, logger)

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
            SquashMergeStrategy._log_cycle_details(remaining, collapsed_changes, logger)

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
        - DELETEs generally happen first to free unique constraints
        - UPDATEs that remove FK references happen before their associated DELETEs
        - CREATEs happen before UPDATEs/CREATEs that reference them

        Returns: ordered list of CollapsedChange objects
        """
        logger.info(f"Ordering {len(collapsed_changes)} collapsed changes...")

        # Remove skipped objects
        to_process = {}
        skipped = []
        for k, v in collapsed_changes.items():
            if v.final_action == ActionType.SKIP:
                skipped.append(v)
            else:
                to_process[k] = v

        logger.info(f"  {len(skipped)} changes will be skipped (created and deleted in branch)")
        logger.info(f"  {len(to_process)} changes to process")

        if not to_process:
            return []

        # Preemptively detect and split bidirectional FK cycles
        # This may add new UPDATE operations to to_process
        SquashMergeStrategy._split_bidirectional_cycles(to_process, logger)

        # Group by action and sort each group by time - need this to build the
        # dependency graph correctly
        deletes = sorted(
            [v for v in to_process.values() if v.final_action == ActionType.DELETE],
            key=lambda c: c.last_change.time
        )
        updates = sorted(
            [v for v in to_process.values() if v.final_action == ActionType.UPDATE],
            key=lambda c: c.last_change.time
        )
        creates = sorted(
            [v for v in to_process.values() if v.final_action == ActionType.CREATE],
            key=lambda c: c.last_change.time
        )

        SquashMergeStrategy._build_fk_dependency_graph(deletes, updates, creates, logger)
        ordered_keys = SquashMergeStrategy._dependency_order_by_references(to_process, logger)

        # Convert keys back to collapsed changes
        result = [to_process[key] for key in ordered_keys]
        return result
