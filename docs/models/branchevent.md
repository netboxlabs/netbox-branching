# Branch Events

Branch operations, such as syncing and merging, are tracked as events. This record of events serves as a history for each branch.

## Fields

### Time

The time at which the event occurred.

### Branch

The [branch](./branch.md) to which this event pertains.

### User

The NetBox user responsible for triggering this event. This field may be null if the event was triggered by an internal process.

### Type

The type of event. This must be one of the following:

| Type        | Description                                         |
|-------------|-----------------------------------------------------|
| Provisioned | The branch's schema was provisioned in the database |
| Synced      | Changes from main were synchronized into the branch |
| Merged      | Changes from the branch were merged into main       |
| Reverted    | Previously merged changes were reverted             |
