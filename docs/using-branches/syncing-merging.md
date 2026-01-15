# Syncing & Merging Changes

## Syncing a Branch

Synchronizing a branch replicates all recent changes from main into the branch. These changes can be reviewed under the "Changes Behind" tab under the branch view.

To synchronize a branch, click the "Sync" button. (If this button is not visible, verify that the branch status shows "ready" and that you have permission to synchronize the branch.)

!!! warning
    A branch must be synchronized frequently enough to avoid exceeding NetBox's configured [changelog retention period](https://netboxlabs.com/docs/netbox/en/stable/configuration/miscellaneous/#changelog_retention) (which defaults to 90 days). This is to protect against data loss when replicating changes from main. A branch whose `last_sync` time exceeds the configured retention window can no longer be synced.

While a branch is being synchronized, its status will show "synchronizing."

!!! tip
    You can check on the status of the syncing job under the "Jobs" tab of the branch view.

## Merging a Branch

Merging a branch replicates its changes into main, and updates the branch's status to "merged." These changes can be reviewed under the "Changes Ahead" tab under the branch view. Typically, once a branch has been merged, it is no longer used.

To merge a branch, click the "Merge" button. (If this button is not visible, verify that the branch status shows "ready" and that you have permission to merge the branch.)

!!! tip
    To grant non-superusers the ability to merge branches add `merge` under `Additional actions` in `Admin` -> `Authentication` -> `Permissions`

You will be presented with two **merge strategies**:

- **Iterative** - The iterative merge strategy is how branching has always worked. Each change you've made in your branch will be applied to the main branch, maintaining a full changelog record. This is the default and recommended approach.

- **Squash** - Introduced in Branching 0.8.0, the squash merge strategy will combine all changes applied to the same object in your branch into a single change, resulting in smaller changelogs. It can also help recover from certain merge failures (see below).

While a branch is being merged, its status will show "merging."

!!! tip
    You can check on the status of the merging job under the "Jobs" tab of the branch view.

Once a branch has been merged, it can be [reverted](./reverting-a-branch.md), archived, or deleted. Archiving a branch removes its associated schema from the PostgreSQL database to deallocate space. An archived branch cannot be restored, however the branch record is retained for future reference.

### Recovering from Duplicate Object Conflicts

If you find yourself in a situation where identical objects (e.g. sites with the same slug) were created in both main and your branch, the merge will fail due to unique constraint violations. The squash strategy can help you recover:

1. Edit the duplicate object in your branch to use different identifiers (e.g. change the slug from `site_a` to `site_b`)
2. Merge using the squash strategy

Squash will collapse the CREATE and UPDATE into a single CREATE with the new identifiers, allowing the merge to succeed.

## Dealing with Conflicts

In the event an object has been modified in both your branch _and_ in main in a diverging manner, this will be flagged as a conflict. For example, if both you and another user have modified the description of an interface to two different values in main and in the branch, this represents a conflict.

![Screenshot: Branch conflicts](../media/screenshots/branch-conflicts.png)

The good news is that you will be able to proceed with synchronizing or merging your branch even if conflicts exist, however you will need to acknowledge each such conflict to ensure that overwriting the relevant data in your branch with the data from main is acceptable. Do this by selecting each conflict before continuing with the merge.

Alternatively, if the conflicting changes are problematic, you can go back and make the necessary changes in main to avoid overwriting data within your branch.

## Dry Runs

By default, NetBox will perform a "dry run" when synchronizing or merging a branch. This means that it will replicate all the relevant changes to check for errors before ultimately aborting the change and returning the branch to its original state.  To permanently apply these changes instead, check the "commit changes" checkbox.
