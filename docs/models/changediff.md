# Change Diffs

A change diff summarizes the net effect of all changes to a particular NetBox object within a [branch](./branch.md). It simplifies the process of reviewing changes within a branch by collapsing successive individual changes into a single comparison, and is also the model used to detect and surface conflicts between a branch and main.

## Fields

### Branch

The [branch](./branch.md) to which this change pertains.

### Object Type

The content type of the affected NetBox object (e.g. `dcim.site`).

### Object ID

The primary key of the affected object within its content type.

### Object

A generic foreign key resolving to the affected NetBox object. The object is looked up in the branch's schema for active branches and in main for merged or archived branches.

### Object Representation

A snapshot of the object's string representation at the time the change diff was last updated. This is used to render a stable label for the affected object even if the underlying object has since been deleted.

### Action

The type of change made to the object. This must be one of the following:

| Action  | Description                                |
|---------|--------------------------------------------|
| Created | The object was created within the branch   |
| Updated | An existing object was modified            |
| Deleted | The object was deleted within the branch   |

### Original

A snapshot of the object's data prior to any changes (i.e. at the point the branch diverged from main). This is `null` for objects which were created within the branch.

### Modified

A snapshot of the object's data as it appears within the branch. This is `null` for objects which were deleted within the branch.

### Current

A snapshot of the object's data as it currently exists in main. This is `null` if the object no longer exists in main (e.g. it was deleted in main after the branch was created).

### Conflicts

A list of attributes whose values have diverged between the branch and main. For example, if a site's status has been set to different values in both main and in the branch, the `status` attribute will be flagged as a conflict, because adopting either value would overwrite the other.

When this field is non-empty, the conflicts must be explicitly acknowledged by the user before the branch can be synced or merged.

### Last Updated

The time at which this change diff was last updated. Change diffs are refreshed whenever the underlying object changes, either within the branch or in main.
