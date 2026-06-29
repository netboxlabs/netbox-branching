"""
Squash merge strategy implementation with functions for collapsing and ordering ObjectChanges.
"""
from enum import StrEnum

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, models
from netbox.context_managers import event_tracking

from ..error_report import annotate_validation_error
from ..signals import squash_dependency_graph_built
from .strategy import MergeStrategy

__all__ = (
    'SquashMergeStrategy',
)


class ActionType(StrEnum):
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

    @staticmethod
    def _collapse_changes(changes, logger):
        """
        Collapse a queryset of ObjectChanges into a dict of CollapsedChange objects keyed by
        (model_label, object_id). Returns a tuple of (collapsed_changes, change_count).
        """
        collapsed_changes = {}
        change_count = 0

        for change in changes:
            change_count += 1
            app_label, model = change.changed_object_type.natural_key()
            key = (f"{app_label}.{model}", change.changed_object_id)

            if key not in collapsed_changes:
                model_class = change.changed_object_type.model_class()
                collapsed_changes[key] = CollapsedChange(key, model_class)
                logger.debug(f"New object: {model_class.__name__}:{change.changed_object_id}")

            collapsed_changes[key].add_change(change, logger)

        return collapsed_changes, change_count

    @staticmethod
    def _skip_updates_missing_in_main(collapsed_changes, logger):
        """
        Mark any collapsed UPDATE as SKIP if the object no longer exists in main. This handles
        the case where an object was modified in the branch but deleted in main and then synced,
        leaving only an UPDATE in the branch's ObjectChange log with no object to act on.
        """
        for collapsed in collapsed_changes.values():
            if collapsed.final_action == ActionType.UPDATE:
                exists = collapsed.model_class.objects.using(DEFAULT_DB_ALIAS).filter(
                    pk=collapsed.key[1]
                ).exists()
                if not exists:
                    logger.info(
                        f"  Skipping UPDATE for {collapsed.model_class.__name__}:{collapsed.key[1]} "
                        f"(object deleted in main)"
                    )
                    collapsed.final_action = ActionType.SKIP

    def merge(self, branch, changes, request, logger, user):
        """
        Apply changes after collapsing them by object and ordering by dependencies.
        """
        models = set()

        logger.info("Collapsing ObjectChanges by object (incremental)...")
        collapsed_changes, _ = SquashMergeStrategy._collapse_changes(changes, logger)
        SquashMergeStrategy._skip_updates_missing_in_main(collapsed_changes, logger)

        # Order collapsed changes based on dependencies
        ordered_changes = SquashMergeStrategy._order_collapsed_changes(
            collapsed_changes, logger, operation='merge'
        )

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
                try:
                    dummy_change.apply(branch, using=DEFAULT_DB_ALIAS, logger=logger)
                except ValidationError as e:
                    annotate_validation_error(
                        e, model_class,
                        collapsed.last_change.changed_object_id,
                        collapsed.last_change.changed_object_type_id,
                    )
                    raise

        # Perform cleanup tasks
        self._clean(models)

    def revert(self, branch, changes, request, logger, user):
        """
        Undo changes after collapsing them by object and ordering by dependencies.
        """
        models = set()

        logger.info("Collapsing ObjectChanges by object (incremental)...")
        collapsed_changes, change_count = SquashMergeStrategy._collapse_changes(changes, logger)
        logger.info(f"  {change_count} changes collapsed into {len(collapsed_changes)} objects")
        SquashMergeStrategy._skip_updates_missing_in_main(collapsed_changes, logger)

        # Order collapsed changes for revert (reverse of merge order)
        merge_order = SquashMergeStrategy._order_collapsed_changes(
            collapsed_changes, logger, operation='revert'
        )
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
        Handles both regular ForeignKey fields and GenericForeignKey fields.
        """
        if not data:
            return set()

        references = set()

        # Check regular ForeignKey fields
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

        # Check GenericForeignKey fields
        for field in model_class._meta.private_fields:
            if isinstance(field, GenericForeignKey):
                # ObjectChange data may store the CT FK as either 'field_name' or 'field_name_id'
                ct_value = data.get(field.ct_field) or data.get(field.ct_field + '_id')
                fk_value = data.get(field.fk_field)

                if ct_value and fk_value:
                    try:
                        ct = ContentType.objects.get_for_id(ct_value)
                        app_label, model = ct.natural_key()
                        model_label = f"{app_label}.{model}"
                        ref_key = (model_label, fk_value)

                        if ref_key in changed_objects:
                            references.add(ref_key)
                    except ContentType.DoesNotExist:
                        pass

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
    def _iter_create_references(create, creates, ct_cache):
        """
        Yield ``(field_name, target_key, breakable)`` for every concrete ForeignKey and
        GenericForeignKey in ``create.postchange_data`` that points at another object in
        ``creates`` (self-references excluded). Fields are visited in model definition
        order, so the yielded sequence is deterministic.

        ``breakable`` is True only for nullable concrete ForeignKeys — the edges a cycle
        can be deferred at by NULLing the field and restoring it in a follow-up UPDATE.
        GenericForeignKey edges are reported (so cycles through them are detected) but are
        never breakable, mirroring how NetBox persists these relationships at the DB level.

        ``ct_cache`` memoises ContentType natural-key lookups (keyed by model class for
        concrete FKs and by content-type id for GFKs) so repeated graph rebuilds don't
        re-hit Django's ContentType cache once per FK field.
        """
        data = create.postchange_data
        if not data:
            return

        # Concrete ForeignKey fields
        for field in create.model_class._meta.get_fields():
            if not isinstance(field, models.ForeignKey):
                continue
            fk_value = data.get(field.name)
            if not fk_value:
                continue
            natural_key = ct_cache.get(field.related_model)
            if natural_key is None:
                natural_key = ContentType.objects.get_for_model(field.related_model).natural_key()
                ct_cache[field.related_model] = natural_key
            app_label, model = natural_key
            target_key = (f"{app_label}.{model}", fk_value)
            if target_key in creates and target_key != create.key:
                yield field.name, target_key, field.null

        # GenericForeignKey fields
        for field in create.model_class._meta.private_fields:
            if not isinstance(field, GenericForeignKey):
                continue
            # ObjectChange data may store the CT FK as either 'field_name' or 'field_name_id'
            ct_value = data.get(field.ct_field) or data.get(field.ct_field + '_id')
            fk_value = data.get(field.fk_field)
            if not (ct_value and fk_value):
                continue
            natural_key = ct_cache.get(ct_value)
            if natural_key is None:
                try:
                    natural_key = ContentType.objects.get_for_id(ct_value).natural_key()
                except ContentType.DoesNotExist:
                    continue
                ct_cache[ct_value] = natural_key
            app_label, model = natural_key
            target_key = (f"{app_label}.{model}", fk_value)
            if target_key in creates and target_key != create.key:
                yield field.name, target_key, False

    @staticmethod
    def _find_cycle(adjacency):
        """
        Return a single cycle in the directed graph ``adjacency`` (mapping key -> ordered
        iterable of target keys) as a list of keys ``[n0, n1, ..., nk]`` where each
        consecutive pair and the closing pair ``(nk, n0)`` is an edge. Returns None if the
        graph is acyclic. Neighbours are visited in iteration order, so for a given graph
        the cycle returned is deterministic.

        Uses iterative (stack-based) DFS with white/gray/black colouring so deep graphs
        do not hit Python's recursion limit.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {node: WHITE for node in adjacency}
        parent = {}

        for start in adjacency:
            if color[start] != WHITE:
                continue
            color[start] = GRAY
            # Each stack frame stores a *live iterator* over a node's neighbours, not the
            # neighbour list. Revisiting a frame resumes that iterator where it left off
            # (Python iterators return self from __iter__), which is what lets DFS continue
            # past an already-explored child. Do not replace iter(...) with the list itself
            # or copy the frame's iterator — either would restart the scan and break the DFS.
            stack = [(start, iter(adjacency[start]))]
            while stack:
                node, neighbors = stack[-1]
                advanced = False
                for target in neighbors:
                    if target not in color:
                        continue  # target is not itself a tracked CREATE node
                    if color[target] == WHITE:
                        color[target] = GRAY
                        parent[target] = node
                        stack.append((target, iter(adjacency[target])))
                        advanced = True
                        break
                    if color[target] == GRAY:
                        # Back edge node -> target closes a cycle; walk parents back up.
                        cycle = [node]
                        cursor = node
                        while cursor != target:
                            cursor = parent[cursor]
                            cycle.append(cursor)
                        cycle.reverse()
                        return cycle
                if not advanced:
                    color[node] = BLACK
                    stack.pop()

        return None

    @staticmethod
    def _defer_fk(collapsed_changes, create, field_name):
        """
        Break a cycle at ``create``'s nullable FK ``field_name`` by NULLing it on the CREATE
        and emitting a synthetic follow-up UPDATE that restores the original value once the
        referenced object exists. Mirrors how NetBox itself persists self-referential
        topologies such as ``primary_ip4`` / ``Circuit.termination_a``.

        The UPDATE's ``postchange_data`` is a full snapshot, but it is applied as a diff
        (``ObjectChange.apply`` -> ``get_merge_data`` -> ``diff_for_merge`` of pre vs post),
        so only ``field_name`` is actually written. This is what makes deferring multiple
        FKs on the same object safe: a later deferral NULLs another field in this snapshot,
        but because that field is unchanged between this UPDATE's pre/post it is excluded
        from the diff and therefore never clobbers the value restored by its own UPDATE.
        """
        original_postchange = dict(create.postchange_data)
        create.postchange_data[field_name] = None

        # 3-tuple keys distinguish synthetic UPDATEs from real (2-tuple) changes; assert the
        # contract so a future 3-tuple key elsewhere can't silently overwrite a real change.
        update_key = (create.key[0], create.key[1], f'update_{field_name}')
        assert update_key not in collapsed_changes, f"Unexpected key collision: {update_key}"

        update_collapsed = CollapsedChange(update_key, create.model_class)
        update_collapsed.change_count = 1  # Synthetic update from split
        update_collapsed.final_action = ActionType.UPDATE
        update_collapsed.prechange_data = dict(create.postchange_data)
        update_collapsed.postchange_data = original_postchange
        update_collapsed.last_change = create.last_change

        # The UPDATE depends on the originating CREATE existing first. That ordering is also
        # enforced transitively through the cycle (the FK target leads back to this object),
        # but make it direct so _defer_fk doesn't rely on that implicit precondition.
        # _build_fk_dependency_graph runs afterwards and only adds to these sets, so the
        # manual edge survives.
        update_collapsed.depends_on.add(create.key)
        create.depended_by.add(update_key)

        collapsed_changes[update_key] = update_collapsed

    @staticmethod
    def _break_dependency_cycles(collapsed_changes, logger):
        """
        Preemptively detect and break dependency cycles of any length among CREATE
        operations.

        A cycle exists when a set of newly-created objects reference each other in a loop
        via concrete ForeignKeys and/or GenericForeignKeys. Each cycle is broken at an
        eligible edge — one backed by a nullable concrete ForeignKey — by deferring it:
        the field is set NULL on the CREATE and a follow-up UPDATE restores it once the
        referenced object exists (see ``_defer_fk``).

        The two-node case (e.g. Circuit ↔ CircuitTermination) is just the length-2
        specialisation of this routine. The three-node primary-IP loop
        (Device → IPAddress → Interface → Device) is broken at ``Device.primary_ip4``.

        Cycles containing no deferrable (nullable concrete FK) edge are left in place for
        the topological sort to report, as before.
        """
        creates = {key: c for key, c in collapsed_changes.items() if c.final_action == ActionType.CREATE}

        broken_fields = set()  # (create_key, field_name) already deferred — don't redo
        ignored_edges = set()  # unbreakable edges excluded from detection to make progress
        ct_cache = {}  # memoised ContentType natural keys; content types don't change mid-operation
        # Safety bound: every iteration either defers an FK or ignores an edge, both finite.
        max_iterations = 2 * sum(len(c.postchange_data or {}) for c in creates.values()) + len(creates) + 1

        for _ in range(max_iterations):
            # (Re)build the adjacency graph from the current postchange data, so deferred
            # edges drop out automatically once their field has been NULLed. Targets are
            # stored as ordered lists (deduped) so cycle detection is deterministic.
            adjacency = {key: [] for key in creates}
            edge_fields = {}  # (src_key, dst_key) -> list of (field_name, breakable)
            for key, create in creates.items():
                seen_targets = set()
                refs = SquashMergeStrategy._iter_create_references(create, creates, ct_cache)
                for field_name, target_key, breakable in refs:
                    if (key, target_key) in ignored_edges:
                        continue
                    if target_key not in seen_targets:
                        seen_targets.add(target_key)
                        adjacency[key].append(target_key)
                    edge_fields.setdefault((key, target_key), []).append((field_name, breakable))

            cycle = SquashMergeStrategy._find_cycle(adjacency)
            if cycle is None:
                return

            # Find a deferrable edge along the cycle and break it there.
            broke = False
            for i, src in enumerate(cycle):
                dst = cycle[(i + 1) % len(cycle)]
                for field_name, breakable in edge_fields.get((src, dst), ()):
                    if breakable and (src, field_name) not in broken_fields:
                        logger.info(
                            f"  Breaking dependency cycle at {creates[src].model_class.__name__}:{src[1]} "
                            f".{field_name} -> {creates[dst].model_class.__name__}:{dst[1]} "
                            f"(cycle length {len(cycle)})"
                        )
                        SquashMergeStrategy._defer_fk(collapsed_changes, creates[src], field_name)
                        broken_fields.add((src, field_name))
                        broke = True
                        break
                if broke:
                    break

            if not broke:
                # No deferrable edge in this cycle: leave the data untouched so the
                # topological sort reports it, but ignore one of its edges so detection
                # can move on to find any other (breakable) cycles.
                # A cycle always spans at least two distinct nodes (self-references are
                # excluded when building the graph), so cycle[1] is always a valid edge.
                src, dst = cycle[0], cycle[1]
                ignored_edges.add((src, dst))
                logger.warning(
                    f"  Unbreakable dependency cycle (length {len(cycle)}) involving "
                    f"{creates[src].model_class.__name__}:{src[1]}; no nullable FK to defer. "
                    f"Leaving for topological sort to report."
                )

        logger.warning("  Cycle breaking exceeded its iteration bound; remaining cycles left for topological sort.")

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
            identifying_info = [f"{field}={data[field]!r}" for field in ['name', 'slug', 'label'] if field in data]
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
    def _order_collapsed_changes(collapsed_changes, logger, operation):
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

        ``operation`` is forwarded to ``squash_dependency_graph_built`` receivers
        as either ``'merge'`` or ``'revert'`` so they can scope any extra edges
        they add (revert reverses the resulting order, so unconditional edges
        still participate).

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

        # Preemptively detect and break FK dependency cycles of any length.
        # This may add new UPDATE operations to to_process.
        SquashMergeStrategy._break_dependency_cycles(to_process, logger)

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
        squash_dependency_graph_built.send(
            sender=SquashMergeStrategy,
            collapsed_changes=to_process,
            operation=operation,
        )
        ordered_keys = SquashMergeStrategy._dependency_order_by_references(to_process, logger)

        # Convert keys back to collapsed changes
        result = [to_process[key] for key in ordered_keys]
        return result
