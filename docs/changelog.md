# Change Log

## v0.4.0

### Enhancements

* [#52](https://github.com/netboxlabs/nbl-netbox-branching/issues/52) - Introduce the `max_branches` config parameter
* [#71](https://github.com/netboxlabs/nbl-netbox-branching/issues/71) - Ensure the consistent application of logging messages
* [#76](https://github.com/netboxlabs/nbl-netbox-branching/issues/76) - Validate required configuration items on initialization

### Bug Fixes

* [#57](https://github.com/netboxlabs/nbl-netbox-branching/issues/57) - Avoid recording ChangeDiff records for unsupported object types
* [#59](https://github.com/netboxlabs/nbl-netbox-branching/issues/59) - `BranchAwareRouter` should consider branching support for model when determining database connection to use
* [#61](https://github.com/netboxlabs/nbl-netbox-branching/issues/61) - Fix transaction rollback when performing a dry run sync
* [#66](https://github.com/netboxlabs/nbl-netbox-branching/issues/66) - Capture object representation on ChangeDiff when creating a new object within a branch
* [#69](https://github.com/netboxlabs/nbl-netbox-branching/issues/69) - Represent null values for ChangeDiff fields consistently in REST API
* [#73](https://github.com/netboxlabs/nbl-netbox-branching/issues/73) - Ensure all relevant branch diffs are updated when an object is modified in main

---

## v0.3.1

### Bug Fixes

* [#42](https://github.com/netboxlabs/nbl-netbox-branching/issues/42) - Fix exception raised when viewing custom scripts
* [#44](https://github.com/netboxlabs/nbl-netbox-branching/issues/44) - Handle truncated SQL sequence names to avoid exceptions during branch provisioning
* [#48](https://github.com/netboxlabs/nbl-netbox-branching/issues/48) - Ensure background job is terminated in the event branch provisioning errors
* [#50](https://github.com/netboxlabs/nbl-netbox-branching/issues/50) - Branch state should remain as "merged" after dry-run revert

---

## v0.3.0

### Enhancements

* [#2](https://github.com/netboxlabs/nbl-netbox-branching/issues/2) - Enable the ability to revert a previously merged branch
* [#3](https://github.com/netboxlabs/nbl-netbox-branching/issues/3) - Require review & acknowledgment of conflicts before syncing or merging a branch
* [#4](https://github.com/netboxlabs/nbl-netbox-branching/issues/4) - Include a three-way diff summary in the REST API representation of a modified object
* [#13](https://github.com/netboxlabs/nbl-netbox-branching/issues/13) - Add a link to the active branch in the branch selector dropdown
* [#15](https://github.com/netboxlabs/nbl-netbox-branching/issues/15) - Default to performing a "dry run" for branch sync & merge
* [#17](https://github.com/netboxlabs/nbl-netbox-branching/issues/17) - Utilize NetBox's `JobRunner` class for background jobs
* [#29](https://github.com/netboxlabs/nbl-netbox-branching/issues/29) - Register a branch column on NetBox's global changelog table
* [#36](https://github.com/netboxlabs/nbl-netbox-branching/issues/36) - Run the branch provisioning process within an isolated transaction

### Bug Fixes

* [#10](https://github.com/netboxlabs/nbl-netbox-branching/issues/10) - Fix branch merge failure when deleted object was modified in another branch
* [#11](https://github.com/netboxlabs/nbl-netbox-branching/issues/11) - Fix quick search functionality for branch diffs tab
* [#16](https://github.com/netboxlabs/nbl-netbox-branching/issues/16) - Fix support for many-to-many assignments
* [#24](https://github.com/netboxlabs/nbl-netbox-branching/issues/24) - Correct the REST API schema for the sync, merge, and revert branch endpoints
* [#30](https://github.com/netboxlabs/nbl-netbox-branching/issues/30) - Include only unmerged branches with relevant changes in object view notifications
* [#31](https://github.com/netboxlabs/nbl-netbox-branching/issues/31) - Prevent the deletion of a branch in a transitional state

---

## v0.2.0

* Initial private release
