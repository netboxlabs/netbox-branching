# NetBox Branching Best Practices

This document describes the underlying architecture and best practices for using NetBox Branching effectively.

For primary documentation, see the [NetBox Branching Overview](./index.md).

## Core Concepts

### Architecture

NetBox Branching allows you to create copies of NetBox's data model and alter them independently. Changes are reflected only within the branch you're working on until you decide to merge your branch into the main data model.

It is important to understand how the underlying **synchronize** and **merge** functionality operates and how changes are applied to main.

Branching works by replaying NetBox's `ObjectChange` log (changelog) in order, depending on the action:

| Action | Description |
| --- | --- |
| Synchronize | The main changelog is replayed against the data within the branch, from the point of the last sync forward. This is reflected in the **Changes Behind** tab on the branch detail page. |
| Merge | The branch changelog is replayed against the data in main, from the point of branch creation forward. This is reflected in the **Changes Ahead** tab on the branch detail page. |

### When to Work in Branches vs. Main

With this architecture in mind, it is important to decide whether to work in branches or directly in main.

Branching includes conflict resolution, which helps identify objects that have been changed in both main and a branch. This information is presented to the user during sync and merge actions, and users are asked to explicitly accept that they will overwrite the state of some objects. This action is analogous to forcing a merge in Git.

There are scenarios in which conflicts can arise. Some can be recovered from, while others will leave branches unmerge-able.

## Unrecoverable Scenarios

### Editing After Deletion

Consider the following scenario (using Site as an example object, though it applies to any object):

1. A Site is initially created in the `main` branch.

2. A new branch is created.

3. The Site is subsequently updated in this new branch (for example, an attribute is changed).

4. The Site is later deleted in the `main` branch.

**Result:**

This sequence will lead to a merge failure. When the changelog is replayed during the merge process, the system will attempt to apply the update from the branch to a Site that has already been deleted in `main`. Since updating a non-existent (deleted) object is not possible, the merge operation will fail.

## Recoverable Scenarios

### Creating Duplicate Objects

Consider the following scenario, using Site as an example object:

1. A branch is created.

2. In the `main` branch, a Site named "Site A" with the slug "sitea" is created.

3. Instead of synchronizing this change, a separate Site also named "Site A" with the slug "sitea" is independently created in the new branch.

**Result:**

This will lead to a merge failure due to duplicate name and slug. The same problem can occur when identical objects are created across multiple concurrent branches.

**Recovery:**

You can recover by editing the duplicate object in your branch to use different identifiers, then merging with the **squash** strategy. See [Recovering from Duplicate Object Conflicts](./using-branches/syncing-merging.md#recovering-from-duplicate-object-conflicts).

## General Recommendations

Here are recommended best practices for working with NetBox branches:

### General Approach

* **Main or Branches:** Decide whether to work directly in main or in dedicated branches. If working in main, take caution to prevent duplication or deletion of objects that might be concurrently updated in active branches.

* **Conflict Avoidance:** Be mindful of potential conflicts when multiple branches update the same data.

### Branch Management

* **Scope Limitation:** Branches should be limited in scope and exist for only as long as needed to complete the changes. The longer branches are open and the more changes they contain, the more opportunity for conflicts to arise. This is in part due to the complexity of the NetBox object model, alongside the fact that each branch is essentially a copy of the underlying NetBox database.

* **Post-Merge Action:** Once a branch is merged and no longer needed, it should be either archived or deleted.

### Archiving vs. Deletion

* **Archiving:** Archiving removes the branch's PostgreSQL schema (reducing the size of the underlying database and subsequent backups) while retaining the branch record and its event history. An archived branch cannot be reverted.

* **Deletion:** Deletion removes the branch entirely, including all of its event history. This can be useful to avoid confusion around which branches can be reverted, but the branch record will no longer be available for reference.

* **Migration:** A migrated branch (i.e. one whose schema has been brought up to date with main after a NetBox upgrade) behaves no differently from a freshly provisioned branch. If you do not plan to migrate a branch after a NetBox upgrade, archive or delete it instead.
