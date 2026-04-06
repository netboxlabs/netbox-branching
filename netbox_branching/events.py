from django.utils.translation import gettext as _
from netbox.events import EVENT_TYPE_KIND_SUCCESS, EventType

__all__ = (
    'BRANCH_DEPROVISIONED',
    'BRANCH_MERGED',
    'BRANCH_PROVISIONED',
    'BRANCH_REVERTED',
    'BRANCH_SYNCED',
    'add_branch_context',
)

# Branch events
BRANCH_PROVISIONED = 'branch_provisioned'
BRANCH_DEPROVISIONED = 'branch_deprovisioned'
BRANCH_SYNCED = 'branch_synced'
BRANCH_MERGED = 'branch_merged'
BRANCH_REVERTED = 'branch_reverted'


def add_branch_context(events):
    """
    Pre-process queued events to inject active branch context into event data before
    they are dispatched by process_event_queue. Scripts triggered by event rules can
    access branch info via data['active_branch'] (#485).
    """
    for event in events:
        request = event.get('request')
        branch = getattr(request, 'active_branch', None) if request else None
        event['data']['active_branch'] = {
            'id': branch.pk,
            'name': branch.name,
            'schema_id': branch.schema_id,
        } if branch else None


# Register core events
EventType(BRANCH_PROVISIONED, _('Branch provisioned')).register()
EventType(BRANCH_DEPROVISIONED, _('Branch deprovisioned')).register()
EventType(BRANCH_SYNCED, _('Branch synced'), kind=EVENT_TYPE_KIND_SUCCESS).register()
EventType(BRANCH_MERGED, _('Branch merged'), kind=EVENT_TYPE_KIND_SUCCESS).register()
EventType(BRANCH_REVERTED, _('Branch reverted'), kind=EVENT_TYPE_KIND_SUCCESS).register()
