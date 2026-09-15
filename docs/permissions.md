# Permissions

Branches are governed entirely by NetBox's standard [object-based permissions](https://netboxlabs.com/docs/netbox/administration/permissions/). No
branch-specific permission model is introduced: a branch is an object like any other, and the actions which can be
performed on it are expressed as permissions on the `netbox_branching.branch` object type.

## Available Actions

In addition to the usual `view`, `add`, `change`, and `delete` actions, the following branch-specific actions can be
granted:

| Action | Permission | Description |
|---|---|---|
| `sync` | `netbox_branching.sync_branch` | Pull changes from main into the branch |
| `merge` | `netbox_branching.merge_branch` | Apply the branch's changes to main |
| `revert` | `netbox_branching.revert_branch` | Undo a merged branch's changes |
| `migrate` | `netbox_branching.migrate_branch` | Apply outstanding migrations to the branch schema |
| `archive` | `netbox_branching.archive_branch` | Deprovision a merged branch's schema |

A user who has not been granted `view` on any branch sees neither the **Branching** navigation menu nor the branch
selector in the header, and cannot activate a branch.

## Restricting Which Branches a User Can See

Because branches are evaluated as ordinary objects, a permission's **constraints** determine _which_ branches it
applies to. The branch selector, the branch list, the REST API, and branch activation all honor these constraints, so a
user cannot see, select, or work within a branch to which they have not been granted access.

To limit users to the branches they created themselves, assign a permission on Branch with the `view` action and the
following constraints:

```json
{
  "owner": "$user"
}
```

`$user` resolves to the user evaluating the permission. Constraints may reference any field on the Branch model; for
example, tagging branches per team and constraining on the tag:

```json
{
  "tags__slug": "team-fabric"
}
```

Combining the two in a single permission requires both to match. To grant access when _either_ holds, list them as
separate constraint objects:

```json
[
  {"owner": "$user"},
  {"tags__slug": "team-fabric"}
]
```

## Scoping Actions Independently

Each action is evaluated separately, so a contractor can be given full control of their own branch without being able
to merge it into main. Assign one permission with the `view`, `change`, and `sync` actions constrained to
`{"owner": "$user"}`, and withhold `merge` entirely; a reviewer then receives a separate, unconstrained `merge`
permission.

!!! note
    Permissions on the Branch object govern what a user may do _to_ a branch. What a user may change _within_ a branch
    is governed by their permissions on the objects themselves, exactly as it is in main.
