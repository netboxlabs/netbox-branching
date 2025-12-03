# NetBox Branching Best Practices

This document describes the underlying architecture and best practices for using NetBox Branching effectively.

For primary documentation, see the [NetBox Branching Overview](./index.md).

## Core Concepts

### Architecture

NetBox Branching allows you to create copies of NetBox's data model and alter them independently. Changes are reflected only within the branch you're working on until you decide to merge your branch into the main data model.

It is important to understand how the underlying **synchronize** and **merge** functionality operates and how changes are applied to the main branch.

Branching works by replaying the NetBox ObjectChange log (changelog) in order, depending on the action:

| Action | Description |
| --- | --- |
| Synchronize | The main branch changelog is replayed on the data within the branch from the point of the last sync to the branch. This is reflected in the "Changes Behind" tab in the branch detail screen. |
| Merge | The branch changelog is replayed on the data within the main branch from the point of branch creation. This is reflected in the "Changed Ahead" table in the branch detail screen. |

### When to Work in Branches vs. Main

With this architecture in mind, it is important to decide whether to work in branches or in the main branch.

Branching includes conflict resolution, which helps identify objects that have been changed in both the main branch and other branches. This information is presented to the user during sync and merge actions, and users are asked to explicitly accept that they will overwrite the state of some objects. This action is analogous to forcing a merge in Git.

There are scenarios in which conflicts can arise, leading to branches being unmerge-able and un-syncable. These instances should be avoided to prevent issues. They include:

1. When an object is deleted in the main branch and then edited in a branch
2. When two identical objects are created in both the main branch and a branch

## Workflows to Avoid

### Example 1: Editing After Deletion

Consider the following scenario (using Site as an example object, though it applies to any object):

1. A Site is initially created in the `main` branch.

2. A new branch is created.

3. The Site is subsequently updated in this new branch (for example, an attribute is changed).

4. The Site is later deleted in the `main` branch.

**Result:**

This sequence will lead to a merge failure. When the changelog is replayed during the merge process, the system will attempt to apply the update from the branch to a Site that has already been deleted in `main`. Since updating a non-existent (deleted) object is not possible, the merge operation will fail.

### Example 2: Creating Duplicate Objects

Consider the following scenario, using Site as an example object:

1. A branch is created.

2. In the `main` branch, a Site named "Site A" with the slug "sitea" is created.

3. Instead of synchronizing this change, a separate Site also named "Site A" with the slug "sitea" is independently created in the new branch.

**Result:**

This process will inevitably lead to a merge failure. When the changelog is replayed sequentially during the merge, the system will detect a duplicated name and slug combination.

While this example describes a conflict between a single branch and `main`, the same duplicate creation problem can occur across multiple concurrent branches.

### Example 3: Concurrent Creation on Separate Branches

Consider the following scenario involving concurrent site creation on separate branches (this applies to any object type):

1. Branch 1 is created.

2. Branch 2 is created.

3. On Branch 1, a site is created with the name "Site A" and slug "sitea".

4. On Branch 2, a site is also created with the name "Site A" and slug "sitea".

5. Branch 1 is merged successfully.

**Result:**

The outcome of this sequence is a merge failure when attempting to merge Branch 2. This is because the changelog replay process will detect a duplicate site name and slug, as "Site A" and "sitea" already exist in the target due to the successful merge of Branch 1.

## General Recommendations

Here are recommended best practices for working with NetBox branches:

### General Approach

* **Main or Branches:** Decide whether to work directly in `main` or in dedicated branches. If working in `main`, take caution to prevent duplication or deletion of devices that might be concurrently updated in active branches.

* **Conflict Avoidance:** Be mindful of potential conflicts when multiple branches update the same data.

### Branch Management

* **Scope Limitation:** Branches should be limited in scope and exist for only as long as needed to complete the changes. The longer branches are open and the more changes they contain, the more opportunity for conflicts to arise. This is in part due to the complexity of the NetBox object model, alongside the fact that each branch is essentially a copy of the underlying NetBox database.

* **Post-Merge Action:** Once a branch is merged and no longer needed, it should be either archived or deleted.

### Archiving vs. Deletion

* **Archiving:** Archiving prevents the branch from being reverted but maintains the list of changes, however it removes the branch's specific database schema. This action reduces the overall size of the underlying database and subsequent backup sizing.

* **Deletion:** Completely removes the branch. This can be useful to avoid confusion around branches that can be reverted or not.

