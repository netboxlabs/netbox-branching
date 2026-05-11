# Object Changes

This model serves as a [proxy](https://docs.djangoproject.com/en/stable/topics/db/models/#proxy-models) for NetBox's native `ObjectChange` model.

It does not introduce any new database fields. Rather, it implements several methods that assist in the application and reversal of changes from a [branch](./branch.md):

| Method | Purpose |
|--------|---------|
| `apply()` | Apply the recorded change to the target database |
| `undo()` | Reverse a previously applied change |
| `migrate()` | Run any registered change-data migrators for the branch's applied migrations, so that historical changes can still be replayed after a schema migration |

!!! tip
    There is typically no need to use this proxy model in external code. Use NetBox's native `ObjectChange` model instead.
