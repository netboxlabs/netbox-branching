# Branches

A branch represents a divergent state of the NetBox database, isolated within a dedicated PostgreSQL schema. Changes made within a branch do not affect main (or any other branch) until the branch is merged.

## Fields

### Name

The branch's unique name.

### Description

An optional short description of the branch.

### Owner

The NetBox user who created the branch. This value may be null if the owning user account has since been deleted.

### Schema ID

The unique, randomly-generated identifier of the PostgreSQL schema which houses the branch in the database. This is an eight-character alphanumeric string and is generated automatically when the branch is created. The full schema name is the `schema_id` prepended with the configured [`schema_prefix`](../configuration.md#schema_prefix).

### Status

The current status of the branch. This must be one of the following values:

| Status             | Description                                                                                |
|--------------------|--------------------------------------------------------------------------------------------|
| New                | The branch has been created but is not yet provisioned in the database                     |
| Provisioning       | A job is running to provision the branch's PostgreSQL schema                               |
| Ready              | The branch is healthy and available for use                                                |
| Syncing            | A job is running to synchronize changes from main into the branch                          |
| Migrating          | A job is running to apply database migrations to the branch schema                         |
| Merging            | A job is running to merge changes from the branch into main                                |
| Reverting          | A job is running to revert previously merged changes from this branch                      |
| Pending Migrations | One or more database migrations must be applied before the branch can be used              |
| Merged             | The branch's changes have been successfully merged into main                               |
| Archived           | A merged branch whose PostgreSQL schema has been deprovisioned to reclaim space            |
| Failed             | An operation against this branch (typically provisioning or migration) has failed          |

### Applied Migrations

A list of database migrations which have been applied to the branch since it was created. This is used to keep track of which migrations a branch has seen so that historical data can be migrated forward when the branch is synced, merged, or reverted.

### Last Sync

The time at which this branch was most recently synchronized with main. This value will be null if the branch has never been synchronized.

!!! tip
    Reference the `synced_time` attribute on a branch to return either the branch's `last_sync` time or, if null, its creation time.

### Merge Strategy

The strategy used to merge changes from the branch into main. This is set when the branch is merged and cleared when the branch is reverted. The available strategies are:

| Strategy  | Description                                                                          |
|-----------|--------------------------------------------------------------------------------------|
| Iterative | Replays every `ObjectChange` from the branch onto main in chronological order        |
| Squash    | Collapses all changes to each object into a single net operation before applying it  |

See [Syncing & Merging Changes](../using-branches/syncing-merging.md#merge-strategies) for guidance on choosing a strategy.

### Merged Time

The time at which the branch was merged into main. This value will be null if the branch has not been merged (or if a previous merge has since been reverted).

### Merged By

The NetBox user who merged the branch. This value will be null if the branch has not been merged. It may also be null if the user account has been deleted since the branch was merged.

### Comments

Long-form Markdown-formatted comments associated with the branch. (Inherited from `PrimaryModel`.)

### Tags

NetBox tags associated with the branch. (Inherited from `PrimaryModel`.)
