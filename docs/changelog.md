# Change Log

## v0.6.1

### Bug Fixes

* [#246](https://github.com/netboxlabs/netbox-branching/issues/246) - Ensure changelog is retrieved from the correct database schema
* [#299](https://github.com/netboxlabs/netbox-branching/issues/299) - Avoid `AttributeError` exception when provisioning a branch due to invalid content type

---

## v0.6.0

### Enhancements

* [#1](https://github.com/netboxlabs/netbox-branching/issues/1) - Enable the application of database migrations to open branches
* [#122](https://github.com/netboxlabs/netbox-branching/issues/122) - Implement a mechanism to enforce policy before executing branch actions
* [#181](https://github.com/netboxlabs/netbox-branching/issues/181) - Introduce the `main_schema` configuration parameter to set the default schema name
* [#189](https://github.com/netboxlabs/netbox-branching/issues/189) - Introduce a registration mechanism for pre-action branch validators

### Bug Fixes

* [#204](https://github.com/netboxlabs/netbox-branching/issues/204) - Hide navigation menu items for unauthenticated users

---

## v0.5.7

### Bug Fixes

* [#136](https://github.com/netboxlabs/netbox-branching/issues/136) - Set GenericForeignKey values when restoring a deleted object
* [#286](https://github.com/netboxlabs/netbox-branching/issues/286) - Fix display of branch status in selector dropdown

---

## v0.5.6

### Enhancements

* [#262](https://github.com/netboxlabs/netbox-branching/issues/262) - Copy migrations table when provisioning a branch

### Bug Fixes

* [#256](https://github.com/netboxlabs/netbox-branching/issues/256) - Fix "changes ahead" count on branches pending provisioning
* [#260](https://github.com/netboxlabs/netbox-branching/issues/260) - Ignore duplicate SQL indexes when provisioning a branch
* [#275](https://github.com/netboxlabs/netbox-branching/issues/275) - Set `sync_time` on branch during initial provisioning

---

## v0.5.5

### Bug Fixes

* [#242](https://github.com/netboxlabs/netbox-branching/issues/242) - Use RestrictedQuerySet for BranchEvent objects
* [#243](https://github.com/netboxlabs/netbox-branching/issues/243) - Defer MPTT recalculation until all changes have been applied
* [#251](https://github.com/netboxlabs/netbox-branching/issues/251) - Preserve SQL index names when provisioning a branch

---

## v0.5.4

### Bug Fixes

* [#169](https://github.com/netboxlabs/netbox-branching/issues/169) - Fix global search caching function when a branch is active
* [#179](https://github.com/netboxlabs/netbox-branching/issues/179) - Avoid nullifying object representation when updating a ChangeDiff
* [#222](https://github.com/netboxlabs/netbox-branching/issues/222) - Avoid off-screen overflow of long branch names
* [#225](https://github.com/netboxlabs/netbox-branching/issues/225) - Branch status field should not be required in REST API serializer
* [#227](https://github.com/netboxlabs/netbox-branching/issues/227) - Fix scripts triggered via an event rule when the branching plugin is installed

---

## v0.5.3

### Enhancements

* [#209](https://github.com/netboxlabs/netbox-branching/issues/209) - Prevent merging branches whose `last_sync` time exceeds the configured changelog retention window

### Bug Fixes

* [#87](https://github.com/netboxlabs/netbox-branching/issues/87) - Deactivate the active branch (if any) when creating a new branch
* [#148](https://github.com/netboxlabs/netbox-branching/issues/148) - Fix `IntegrityError` exception raised when executing custom scripts within a branch
* [#178](https://github.com/netboxlabs/netbox-branching/issues/178) - Fix display of assigned tags in the branches list

---

## v0.5.2

### Bug Fixes

* [#163](https://github.com/netboxlabs/netbox-branching/issues/163) - Ensure changelog records for non-branching models are created in main schema

---

## v0.5.1

### Enhancements

* [#123](https://github.com/netboxlabs/netbox-branching/issues/123) - Introduce template tags for branch action buttons
* [#129](https://github.com/netboxlabs/netbox-branching/issues/129) - Implement pre-event signals for branch actions

### Bug Fixes

* [#98](https://github.com/netboxlabs/netbox-branching/issues/98) - Cable changes in branch should not impact main schema
* [#119](https://github.com/netboxlabs/netbox-branching/issues/119) - Fix the dynamic selection of related objects in forms while a branch is active
* [#120](https://github.com/netboxlabs/netbox-branching/issues/120) - `max_branches` config parameter should disregard archived branches
* [#138](https://github.com/netboxlabs/netbox-branching/issues/138) - Fix rendering the ID column of the change diffs table
* [#140](https://github.com/netboxlabs/netbox-branching/issues/140) - Fix representation of branch status in REST API
* [#142](https://github.com/netboxlabs/netbox-branching/issues/142) - Fix tab record counts for archived branches

---

## v0.5.0

### Enhancements

* [#83](https://github.com/netboxlabs/netbox-branching/issues/83) - Add a "share" button under object views when a branch is active
* [#84](https://github.com/netboxlabs/netbox-branching/issues/84) - Introduce the `max_working_branches` configuration parameter
* [#88](https://github.com/netboxlabs/netbox-branching/issues/88) - Add branching support for NetBox's graphQL API
* [#90](https://github.com/netboxlabs/netbox-branching/issues/90) - Introduce the ability to archive & deprovision merged branches without deleting them
* [#97](https://github.com/netboxlabs/netbox-branching/issues/97) - Introduce the `exempt_models` config parameter to disable branching support for plugin models
* [#116](https://github.com/netboxlabs/netbox-branching/issues/116) - Disable branching support for applicable core models

### Bug Fixes

* [#81](https://github.com/netboxlabs/netbox-branching/issues/81) - Fix event rule triggering for the `branch_reverted` event
* [#91](https://github.com/netboxlabs/netbox-branching/issues/91) - Disregard the active branch (if any) when alerting on changes under object views
* [#94](https://github.com/netboxlabs/netbox-branching/issues/94) - Fix branch merging after modifying an object with custom field data
* [#101](https://github.com/netboxlabs/netbox-branching/issues/101) - Permit (but warn about) database queries issued before branching support has been initialized
* [#102](https://github.com/netboxlabs/netbox-branching/issues/102) - Record individual object actions in branch job logs

---

## v0.4.0

### Enhancements

* [#52](https://github.com/netboxlabs/netbox-branching/issues/52) - Introduce the `max_branches` config parameter
* [#71](https://github.com/netboxlabs/netbox-branching/issues/71) - Ensure the consistent application of logging messages
* [#76](https://github.com/netboxlabs/netbox-branching/issues/76) - Validate required configuration items on initialization

### Bug Fixes

* [#57](https://github.com/netboxlabs/netbox-branching/issues/57) - Avoid recording ChangeDiff records for unsupported object types
* [#59](https://github.com/netboxlabs/netbox-branching/issues/59) - `BranchAwareRouter` should consider branching support for model when determining database connection to use
* [#61](https://github.com/netboxlabs/netbox-branching/issues/61) - Fix transaction rollback when performing a dry run sync
* [#66](https://github.com/netboxlabs/netbox-branching/issues/66) - Capture object representation on ChangeDiff when creating a new object within a branch
* [#69](https://github.com/netboxlabs/netbox-branching/issues/69) - Represent null values for ChangeDiff fields consistently in REST API
* [#73](https://github.com/netboxlabs/netbox-branching/issues/73) - Ensure all relevant branch diffs are updated when an object is modified in main

---

## v0.3.1

### Bug Fixes

* [#42](https://github.com/netboxlabs/netbox-branching/issues/42) - Fix exception raised when viewing custom scripts
* [#44](https://github.com/netboxlabs/netbox-branching/issues/44) - Handle truncated SQL sequence names to avoid exceptions during branch provisioning
* [#48](https://github.com/netboxlabs/netbox-branching/issues/48) - Ensure background job is terminated in the event branch provisioning errors
* [#50](https://github.com/netboxlabs/netbox-branching/issues/50) - Branch state should remain as "merged" after dry-run revert

---

## v0.3.0

### Enhancements

* [#2](https://github.com/netboxlabs/netbox-branching/issues/2) - Enable the ability to revert a previously merged branch
* [#3](https://github.com/netboxlabs/netbox-branching/issues/3) - Require review & acknowledgment of conflicts before syncing or merging a branch
* [#4](https://github.com/netboxlabs/netbox-branching/issues/4) - Include a three-way diff summary in the REST API representation of a modified object
* [#13](https://github.com/netboxlabs/netbox-branching/issues/13) - Add a link to the active branch in the branch selector dropdown
* [#15](https://github.com/netboxlabs/netbox-branching/issues/15) - Default to performing a "dry run" for branch sync & merge
* [#17](https://github.com/netboxlabs/netbox-branching/issues/17) - Utilize NetBox's `JobRunner` class for background jobs
* [#29](https://github.com/netboxlabs/netbox-branching/issues/29) - Register a branch column on NetBox's global changelog table
* [#36](https://github.com/netboxlabs/netbox-branching/issues/36) - Run the branch provisioning process within an isolated transaction

### Bug Fixes

* [#10](https://github.com/netboxlabs/netbox-branching/issues/10) - Fix branch merge failure when deleted object was modified in another branch
* [#11](https://github.com/netboxlabs/netbox-branching/issues/11) - Fix quick search functionality for branch diffs tab
* [#16](https://github.com/netboxlabs/netbox-branching/issues/16) - Fix support for many-to-many assignments
* [#24](https://github.com/netboxlabs/netbox-branching/issues/24) - Correct the REST API schema for the sync, merge, and revert branch endpoints
* [#30](https://github.com/netboxlabs/netbox-branching/issues/30) - Include only unmerged branches with relevant changes in object view notifications
* [#31](https://github.com/netboxlabs/netbox-branching/issues/31) - Prevent the deletion of a branch in a transitional state

---

## v0.2.0

* Initial private release
