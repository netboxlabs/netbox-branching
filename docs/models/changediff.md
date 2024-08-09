# Change Diffs

A change diff summarized all changes to particular NetBox object within a [branch](./branch.md). It serves to simplify the process of reviewing changes within a branch, and avoids the need to review successive individual changes which might otherwise prove tedious.

## Fields

### Branch

The [branch](./branch.md) to which this change pertains.

### Object

The NetBox object to which this change pertains.

### Action

The type of change. This must be one of the following:

* Created
* Updated
* Deleted

### Original Data

A snapshot of the object prior to the change.

### Modified Data

A snapshot of the object as it has been modified within the branch.

### Current Data

A snapshot of the object as it currently exists in main.

### Conflicts

A list of attributes with conflicting values. For example, if a site's status has been changed to different values in both main and in the branch, this will be flagged as a conflict: Adopting the new value from either version would overwrite the other.
