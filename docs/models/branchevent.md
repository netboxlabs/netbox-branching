# Branch Events

Branch lifecycle operations (provisioning, syncing, merging, etc.) are recorded as branch events. The list of events associated with a branch serves as its operational history.

## Fields

### Time

The time at which the event occurred.

### Branch

The [branch](./branch.md) to which this event pertains.

### User

The NetBox user responsible for triggering this event. This field may be null if the event was triggered by an internal process or by a user account that has since been deleted.

### Type

The type of event. This must be one of the following:

| Type        | Description                                                            |
|-------------|------------------------------------------------------------------------|
| Provisioned | The branch's schema was provisioned in the database                    |
| Synced      | Changes from main were synchronized into the branch                    |
| Migrated    | Database migrations were applied to the branch schema                  |
| Merged      | Changes from the branch were merged into main                          |
| Reverted    | Previously merged changes from the branch were reverted in main        |
| Archived    | The branch's schema was deprovisioned, but its event record was kept   |
