from django.utils.translation import gettext_lazy as _

from netbox.ui import attrs
from netbox_branching.models import ChangeDiff

__all__ = (
    'ConflictsAttr',
    'LatestChangeAttr',
)


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
