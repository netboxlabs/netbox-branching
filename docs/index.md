# NetBox Branching

[NetBox](https://github.com/netbox-community/netbox) is the world's leading source of truth for network infrastructure, featuring an extensive and complex data model. But sometimes it can be challenging to orchestrate changes, especially when working within a large team. This plugin introduces a new paradigm for NetBox to help overcome these challenges: branching.

If you're familiar with [git](https://git-scm.com/) or similar version control systems, the concept of branching should be familiar. Essentially, this plugin allows you to make copies of NetBox's data model and alter them independently. Your changes will be reflected only within the branch you're working on, until you decide to merge your branch into the main data model.

This allows you and your colleagues to stage changes within isolated environments and avoid interfering with one another's work or pushing changes to the network prematurely. Each branch can be synchronized as needed to keep up to date with external changes, and merged when needed.

## Features

* Users can create new branches and switch between them seamlessly while navigating the web UI.

* Each branch exists in isolation from its peers: Changes made within one branch won't affect any other branches.

* Standard NetBox permissions are employed to control which users can perform branch operations.

* Branches can be created, synchronized, merged, reverted, and deleted through the REST API.

* No external dependencies! This plugin requires only NetBox v4.1 or later and a conventional PostgreSQL database (v12.0 or later).

## Terminology

* **Main** is shorthand for the primary NetBox state. Any changes made outside the context of a specific branch are made here.

* The creation, modification, or deletion of an object is a **change**.

* A **branch** is an independent copy of the NetBox data model which diverges from main at a set point in time. Any changes to main after that time will not be reflected in the branch. Likewise, changes made within the branch will not be reflected in main.

* Branches are **provisioned** automatically upon creation. The initial state of a branch is identical to the state of main at the time it was provisioned. 

* Changes in main can be **synchronized** at any time into a branch. Branches are independent of one another: Changes must be synchronized into each branch individually. This ensures complete isolation among branches.

* Once the work within a branch has been completed, it can be **merged** into main. Once a branch has been merged, it is generally no longer used.

* Merged changes can be **reverted** provided the branch has not yet been deleted. This effectively replays the changes in reverse order to undo the relevant changes.

## Workflow

The first step is to [create a new branch](./using-branches/creating-a-branch.md). Upon creation, a background job is automatically queued to provision a dedicated PostgreSQL schema for the branch. When provisioning is complete, the branch's status is updated to "ready."

Users can now activate the branch and begin making changes within it. These changes will be contained to the branch, and will not impact main. Likewise, any changes to main will not be reflected in the branch until it has been [synchronized](./using-branches/syncing-merging.md#syncing-a-branch) by a user. A branch may be synchronized repeatedly to keep it up to date with main over time.

Once work in the branch has been completed, it can be [merged](./using-branches/syncing-merging.md#merging-a-branch) into main.

```mermaid
sequenceDiagram
    actor User B
    participant Main
    participant Branch
    actor User A
    Main->>Branch: Provision new branch
    User A->>Branch: Make changes
    User B->>Main: Make unrelated changes
    Main->>Branch: Synchronize changes
    User A->>Branch: Make more changes
    Branch->>Main: Merge branch
```

In the event a branch should not have been merged, it can be reverted. Previously merged changes to main will be unwound and the branch will be restored to its pre-merge state. The branch is again marked as ready for additional changes, if needed, and can be merged again.

```mermaid
sequenceDiagram
    participant Main
    participant Branch
    actor User A
    Main->>Branch: Provision new branch
    User A->>Branch: Make changes
    Branch->>Main: Merge branch
    Note left of Main: Error detected!
    Main->>Branch: Revert changes
    User A->>Branch: Correct error
    Branch->>Main: Merge branch
```

## Getting Started

TODO

## Known Limitations

There are currently a few limitations to the functionality provided by this plugin that are worth highlighting. We hope to address these in future releases.

* **Branches may not persist across minor version upgrades of NetBox.** Users are strongly encouraged to merge or remove all open branches prior to upgrading to a new minor release of NetBox (e.g. from v4.1 to v4.2). This is because database migrations introduced by the upgrade will _not_ be applied to branch schemas, potentially resulting in an invalid state. However, it should be considered safe to upgrade to new patch releases (e.g. v4.1.0 to v4.1.1) with open branches.

* **Open branches will not reflect newly installed plugins.** Any branches created before installing a new plugin will not be updated to support its models. Note, however, that installing a new plugin will generally not impede the use of existing branches. Users are encouraged to install all necessary plugins prior to creating branches. (This also applies to database migrations introduced by upgrading a plugin.)

* **Changes to main can potentially disrupt the branch provisioning process.**  Changes made to the main schema while a branch is being provisioned can potentially be only partially captured, and may result in an incomplete or invalid state being copied to the branch. Users are encouraged to avoid making changes to the main schema while a branch is being provisioned. (Changes made to other branches, however, will not interfere with this process.)
