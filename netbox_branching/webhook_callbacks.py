from django.core.exceptions import ObjectDoesNotExist

from extras.webhooks import register_webhook_callback
from netbox_branching.utilities import BranchNotReady, get_active_branch


@register_webhook_callback
def set_active_branch(object_type, event_type, data, request):
    if request is None:
        return None
    # A webhook callback cannot refuse the request, so a branch which is unready or absent is
    # reported as no active branch rather than propagating out of the callback.
    try:
        branch = get_active_branch(request)
    except (BranchNotReady, ObjectDoesNotExist):
        branch = None
    attrs = {'id': branch.pk, 'name': branch.name, 'schema_id': branch.schema_id} if branch else None
    return {
        'active_branch': attrs,
    }
