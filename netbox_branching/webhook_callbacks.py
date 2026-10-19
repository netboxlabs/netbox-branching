from extras.webhooks import register_webhook_callback

from netbox_branching.utilities import get_active_branch


@register_webhook_callback
def set_active_branch(object_type, event_type, data, request):
    if request is None:
        return None
    if branch := get_active_branch(request):
        attrs = {
            'id': branch.pk,
            'name': branch.name,
            'backend_id': branch.backend_id,
        }
    else:
        attrs = None
    return {
        'active_branch': attrs,
    }
