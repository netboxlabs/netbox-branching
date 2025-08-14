from extras.webhooks import register_webhook_callback

from netbox_branching.utilities import get_active_branch


@register_webhook_callback
def set_active_branch(object_type, event_type, data, request):
    if request is None:
        return
    if branch := get_active_branch(request):
        attrs = {
            'id': branch.pk,
            'name': branch.name,
            'schema_id': branch.schema_id,
        }
    else:
        attrs = None
    return {
        'active_branch': attrs,
    }
