# Using the REST API

This plugin includes support for activating and deactivating branches via the REST API in addition to conventional creation, modification, and deletion operations.

!!! tip "API Token Required"
    You'll need a valid NetBox REST API token to follow any of the examples shown here. API tokens can be provisioned by navigating to the API tokens list in the user menu.

## Creating a Branch

Branches are created in a manner similar to most objects in NetBox. A `POST` request (including a valid authentication token) is sent to the `branches/` API endpoint with the desired attributes, such as name and description:

```no-highlight title="Request"
curl -X POST \
-H "Authorization: Token $TOKEN" \
-H "Content-Type: application/json" \
-H "Accept: application/json; indent=4" \
http://netbox:8000/api/plugins/branching/branches/ \
--data '{"name": "Branch 1", "description": "My new branch"}'
```

```json title="Response"
{
    "id": 2,
    "url": "http://netbox:8000/api/plugins/branching/branches/2/",
    "display": "Branch 1",
    "name": "Branch 1",
    "status": "new",
    "owner": {
        "id": 1,
        "url": "http://netbox:8000/api/users/users/1/",
        "display": "admin",
        "username": "admin"
    },
    "description": "My new branch",
    "schema_id": "td5smq0f",
    "last_sync": null,
    "merged_time": null,
    "merged_by": null,
    "comments": "",
    "tags": [],
    "custom_fields": {},
    "created": "2024-08-12T17:07:46.196956Z",
    "last_updated": "2024-08-12T17:07:46.196970Z"
}
```

Once a new branch has been created, it will be provisioned automatically, just as when one is created via the web UI. The branch's status will show "ready" when provisioning has completed.

Once provisioned, branches can be modified and deleted via the `/api/plugins/branching/branches/<id>/` endpoint, similar to most objects in NetBox.

## Activating a Branch

Unlike the web UI, where a user's selected branch remains active until it is changed, the desired branch must be specified with each REST API request. This is accomplished by including the `X-NetBox-Branch` HTTP header specifying the branch's schema ID.

```no-highlight
X-NetBox-Branch: $SCHEMA_ID
```

!!! tip "Schema IDs"
    The schema ID for a branch can be found in its REST API representation or on its detail view in the web UI. This is a pseudorandom eight-character alphanumeric identifier generated automatically when a branch is created. Note that the value passed to the HTTP header **does not include** the `branch_` prefix, which comprises part of the schema's name in the underlying database.

The example below returns all site objects that exist within the branch with schema ID `td5smq0f`:

```no-highlight title="Request"
curl -X POST \
-H "Authorization: Token $TOKEN" \
-H "Content-Type: application/json" \
-H "Accept: application/json; indent=4" \
-H "X-NetBox-Branch: td5smq0f" \
http://netbox:8000/api/dcim/sites/
```

The branch is effectively "deactivated" for future API requests by simply omitting the header.

!!! note
    The `X-NetBox-Branch` header is required only when making changes to NetBox objects within the context of an active branch. It is **not** required when creating, modifying, or deleting a branch itself.

## Syncing & Merging Branches

Several REST API endpoints are provided to handle synchronizing, merging, and reverting branches:

| Endpoint                                      | Description                                 |
|-----------------------------------------------|---------------------------------------------|
| `/api/plugins/branching/branches/<id>/sync/`   | Synchronize changes from main to the branch |
| `/api/plugins/branching/branches/<id>/merge/`  | Merge a branch into main                    |
| `/api/plugins/branching/branches/<id>/revert/` | Revert a previously merged branch           |

To synchronize updates from main into a branch, send a `POST` request to the desired branch's `sync/` endpoint.

This endpoint requires a `commit` argument: Setting this to `false` effects a dry-run, where the changes to the branch are automatically rolled back at the end of the job. (This can be helpful to check for potential errors before committing to a set of changes.)

```no-highlight title="Request"
curl -X POST \
-H "Authorization: Token $TOKEN" \
-H "Content-Type: application/json" \
-H "Accept: application/json; indent=4" \
http://netbox:8000/api/plugins/branching/branches/2/sync/ \
--data '{"commit": true}'
```

If successful, this will return data about the background job that has been enqueued to handle the synchronization of data. This job can be queried to determine the progress of the synchronization.

```json title="Response"
{
    "id": 4,
    "url": "http://netbox:8000/api/core/jobs/4/",
    "display_url": "http://netbox:8000/core/jobs/4/",
    "display": "f0c6dea2-d5bb-4683-851e-2ac705510af4",
    "object_type": "netbox_branching.branch",
    "object_id": 2,
    "name": "Sync branch",
    "status": {
        "value": "pending",
        "label": "Pending"
    },
    "created": "2024-08-12T17:27:57.448405Z",
    "scheduled": null,
    "interval": null,
    "started": null,
    "completed": null,
    "user": {
        "id": 1,
        "url": "http://netbox:8000/api/users/users/1/",
        "display": "admin",
        "username": "admin"
    },
    "data": null,
    "error": "",
    "job_id": "f0c6dea2-d5bb-4683-851e-2ac705510af4"
}
```

This same pattern can be followed to merge and revert branches via their respective API endpoints, listed above.
