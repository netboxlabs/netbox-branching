# Reverting a Branch

Once a branch has been merged, it is generally no longer needed, and can no longer be activated. However, occasionally you may find it necessary to undo the changes from a branch (due to an error or an otherwise undesired state). This can be done by _reverting_ the branch. Only merged branches can be reverted.

!!! warning
    Only branches which have not yet been archived or deleted can be reverted. Once a branch's schema has been deprovisioned, it can no longer be reverted.

Before reverting a branch, review the changes listed under its "Merged Changes" tab. NetBox will attempt to undo these specific changes when reverting the branch.

To revert a merged branch, click the "Revert" button. You will be asked to review the changes and to acknowledge any conflicts before executing the reversion. Continuing with the merge will queue a background job to carry out reverting the changes. When the job is running, the branch's status will show "reverting."

!!! tip
    You can check on the status of the reversion job under the "Jobs" tab of the branch view.

Once the reversion has completed, the branch will be returned to its pre-merge status, and will again be available to activate. Its event history will show that the branch has been reverted.

## A Note on Change Logging

Reverting a merged branch does _not_ erase any records from the global change log. The original changes resulting from the initial branch merge will be retained, and _new_ change records signifying the inverse of those changes will be added. So, if you're hoping to cover your tracks after doing something foolish, reverting a branch won't help you. But it does provide a convenient path for backing out an undesirable change.

For example, suppose you made three changes within a branch before merging it:

1. Create site A
2. Change the description of device B from "foo" to "bar"
3. Delete tenant C

Reverting the branch will apply the following changes, in this order:

1. Create tenant C with its original attributes
2. Change the description of device B from "bar" to "too"
3. Delete site A

After reverting the branch, the global change log will include a record for each of the six discrete changes.
