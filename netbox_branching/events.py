from django.utils.translation import gettext as _

from netbox.events import EventType, EVENT_TYPE_KIND_SUCCESS

__all__ = (
    'BRANCH_DEPROVISIONED',
    'BRANCH_MERGED',
    'BRANCH_PROVISIONED',
    'BRANCH_SYNCED',
)

# Branch events
BRANCH_PROVISIONED = 'branch_provisioned'
BRANCH_SYNCED = 'branch_synced'
BRANCH_MERGED = 'branch_merged'
BRANCH_DEPROVISIONED = 'branch_deprovisioned'


# Register core events
EventType(BRANCH_PROVISIONED, _('Branch provisioned'), kind=EVENT_TYPE_KIND_SUCCESS).register()
EventType(BRANCH_SYNCED, _('Branch synced'), kind=EVENT_TYPE_KIND_SUCCESS).register()
EventType(BRANCH_MERGED, _('Branch merged'), kind=EVENT_TYPE_KIND_SUCCESS).register()
EventType(BRANCH_DEPROVISIONED, _('Branch deprovisioned'), kind=EVENT_TYPE_KIND_SUCCESS).register()
