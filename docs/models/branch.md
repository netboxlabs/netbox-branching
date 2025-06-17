# Branches

A branch represents a divergent state from the main database.

## Fields

### Name

The branch's unique name.

### Owner

The NetBox user who created the branch.

### Schema ID

The unique, randomly-generated identifier of the PostgreSQL schema which houses the branch in the database.

### Status

The current status of the branch. This must be one of the following values.

| Status             | Description                                                                   |
|--------------------|-------------------------------------------------------------------------------|
| New                | Not yet provisioned in the database                                           |
| Provisioning       | A job is running to provision the branch's PostgreSQL schema                  |
| Ready              | The branch is healthy and ready to be synchronized or merged                  |
| Syncing            | A job is running to synchronize changes from main into the branch             |
| Migrating          | A job is running to apply database migrations to the branch schema            |
| Merging            | A job is running to merge changes from the branch into main                   |
| Reverting          | A job is running to revert previously merged changes in main                  |
| Pending Migrations | One or more database migrations must be applied before the branch can be used |
| Merged             | Changes from this branch have been successfully merged into main              |
| Archived           | A merged branch which has been deprovisioned in the database                  |
| Failed             | Provisioning the schema for this branch has failed                            |

### Applied Migrations

A list of database migrations which have been applied to the branch since it was created. This may be necessary to keep open branches up to date during NetBox upgrades.

### Last Sync

The time at which this branch was most recently synchronized with main. This value will be null if the branch has never been synchronized.

!!! tip
    Reference the `synced_time` attribute on a branch to return either the branch's `last_sync` time or, if null, its creation time.

### Merged Time

The time at which the branch was merged into main. This value will be null if the branch has not been merged.

### Merged By

The NetBox user who merged the branch. This value will be null if the branch has not been merged.
