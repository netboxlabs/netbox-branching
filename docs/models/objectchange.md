# Object Changes

This model serves as a [proxy](https://docs.djangoproject.com/en/stable/topics/db/models/#proxy-models) for NetBox's native `ObjectChange` model.

It does not introduce any new database fields. Rather, it implements several functions which assist in the application and reversal of changes from a [branch](./branch.md) (namely `apply()` and `undo()`).

!!! tip
    There is typically no need to employ this model in external code. Use the NetBox's native `ObjectChange` model instead.
