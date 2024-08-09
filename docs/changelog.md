# Change Log

## v0.3.0

### Enhancements

* [#2](https://github.com/netboxlabs/nbl-netbox-branching/issues/2) - Enable the ability to revert a previously merged branch
* [#3](https://github.com/netboxlabs/nbl-netbox-branching/issues/3) - Require review & acknowledgment of conflicts before syncing or merging a branch
* [#4](https://github.com/netboxlabs/nbl-netbox-branching/issues/4) - Include a three-way diff summary in the REST API representation of a modified object
* [#13](https://github.com/netboxlabs/nbl-netbox-branching/issues/13) - Add a link to the active branch in the branch selector dropdown
* [#15](https://github.com/netboxlabs/nbl-netbox-branching/issues/15) - Default to performing a "dry run" for branch sync & merge
* [#17](https://github.com/netboxlabs/nbl-netbox-branching/issues/17) - Utilize NetBox's `JobRunner` class for background jobs

### Bug Fixes

* [#10](https://github.com/netboxlabs/nbl-netbox-branching/issues/10) - Fix branch merge failure when deleted object was modified in another branch
* [#11](https://github.com/netboxlabs/nbl-netbox-branching/issues/11) - Fix quick search functionality for branch diffs tab
* [#16](https://github.com/netboxlabs/nbl-netbox-branching/issues/16) - Fix support for many-to-many assignments
* [#30](https://github.com/netboxlabs/nbl-netbox-branching/issues/30) - Include only unmerged branches with relevant changes in object view notifications

## v0.2.0

* Initial private release
