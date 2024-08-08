from django.utils.translation import gettext as _

from netbox.events import EventType, EVENT_TYPE_KIND_SUCCESS

__all__ = (
    'BRANCH_DEPROVISIONED',
    'BRANCH_MERGED',
    'BRANCH_PROVISIONED',
    'BRANCH_REVERTED',
    'BRANCH_SYNCED',
)

# Branch events
BRANCH_PROVISIONED = 'branch_provisioned'
BRANCH_DEPROVISIONED = 'branch_deprovisioned'
BRANCH_SYNCED = 'branch_synced'
BRANCH_MERGED = 'branch_merged'
BRANCH_REVERTED = 'branch_reverted'


# Register core events
EventType(BRANCH_PROVISIONED, _('Branch provisioned')).register()
EventType(BRANCH_DEPROVISIONED, _('Branch deprovisioned')).register()
EventType(BRANCH_SYNCED, _('Branch synced'), kind=EVENT_TYPE_KIND_SUCCESS).register()
EventType(BRANCH_MERGED, _('Branch merged'), kind=EVENT_TYPE_KIND_SUCCESS).register()
EventType(BRANCH_REVERTED, _('Branch reverted'), kind=EVENT_TYPE_KIND_SUCCESS).register()
