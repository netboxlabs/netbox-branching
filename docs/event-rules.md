# Event Rules

When `add_branch_context` is configured in NetBox's `EVENTS_PIPELINE` (see [configuration](configuration.md)), branch context is available throughout event rule processing. This applies to all action types — webhooks, scripts, and notifications.

- **Scripts** can access branch info via `data.get('active_branch')`
- **Webhooks** receive the `active_branch` key in the posted payload
- **Conditions** can filter on `active_branch` to control whether the rule fires at all

## Conditional Event Rules

Event rule conditions are entered as JSON in the **Conditions** field when creating or editing a rule under **Integrations > Event Rules**. The following examples use `active_branch` to filter by branch context.

Only fire when a change was made on a branch (any branch):

```json
{
    "and": [
        {"attr": "active_branch", "value": null, "negate": true}
    ]
}
```

Only fire when a change was made on main (no active branch):

```json
{
    "and": [
        {"attr": "active_branch", "value": null}
    ]
}
```

Only fire for a specific branch by name:

```json
{
    "and": [
        {"attr": "active_branch.name", "value": "my-branch"}
    ]
}
```

For more detail on NetBox's condition syntax, see the [NetBox conditions reference](https://netboxlabs.com/docs/netbox/en/stable/reference/conditions/).

## Accessing Branch Context in Scripts

Scripts triggered by event rules receive branch info via the `data` parameter:

```python
class MyScript(Script):
    def run(self, data, commit):
        active_branch = data.get('active_branch')
        if active_branch:
            self.log_info(f"Change made on branch: {active_branch['name']}")
        else:
            self.log_info("Change made on main")
```

When a branch is active, `active_branch` contains:

| Field | Description |
|-------|-------------|
| `id` | Branch primary key |
| `name` | Branch name |
| `schema_id` | Branch PostgreSQL schema identifier |
