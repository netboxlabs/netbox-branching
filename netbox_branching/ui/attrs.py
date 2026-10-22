from django.utils.translation import gettext_lazy as _

from core.choices import JobStatusChoices
from netbox.ui import attrs
from netbox_branching.models import ChangeDiff

__all__ = (
    'BranchStatusAttr',
    'ConflictsAttr',
    'LatestChangeAttr',
)


class BranchStatusAttr(attrs.ObjectAttribute):
    """
    A branch's status badge, flagging the branch's most recent job if it failed to complete.
    """

    template_name = 'netbox_branching/attrs/status.html'
    label = _('Status')

    def __init__(self, accessor='status', **kwargs):
        super().__init__(accessor, **kwargs)

    def get_context(self, obj, attr, value, context):
        last_job = obj.jobs.order_by('created').last()
        return {
            'object': obj,
            'failed_job': last_job if last_job and last_job.status == JobStatusChoices.STATUS_FAILED else None,
        }


class LatestChangeAttr(attrs.ObjectAttribute):
    """
    The most recent change recorded within a branch.
    """

    template_name = 'netbox_branching/attrs/latest_change.html'
    label = _('Last activity')

    def __init__(self, accessor=None, **kwargs):
        super().__init__(accessor, **kwargs)

    def get_value(self, obj):
        if not (obj.ready or obj.merged):
            return None
        return obj.get_changes().order_by('time').last()


class ConflictsAttr(attrs.ObjectAttribute):
    """
    The number of unresolved conflicts between a branch and main. Renders a placeholder for branches which are
    not ready.
    """

    template_name = 'netbox_branching/attrs/conflicts.html'
    label = _('Conflicts')

    def __init__(self, accessor=None, **kwargs):
        super().__init__(accessor, **kwargs)

    def get_value(self, obj):
        if not obj.ready:
            return None
        return ChangeDiff.objects.filter(branch=obj, conflicts__isnull=False).count()

    def get_context(self, obj, attr, value, context):
        return {'object': obj}
