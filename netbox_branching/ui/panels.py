from django.contrib.contenttypes.models import ContentType
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from netbox.ui import attrs, panels
from netbox_branching.ui.attrs import ConflictsAttr, LatestChangeAttr

__all__ = (
    'BranchChangeStatsPanel',
    'BranchEventHistoryPanel',
    'BranchPanel',
    'ChangeDiffConflictsPanel',
    'ChangeDiffDataPanel',
    'ChangeDiffDifferencePanel',
    'ChangeDiffPanel',
    'MergeReportFailuresPanel',
    'MergeReportLogPanel',
    'MergeReportRecommendationsPanel',
    'MergeReportSummaryPanel',
)


#
# Branch panels
#


class BranchPanel(panels.ObjectAttributesPanel):
    name = attrs.TextAttr('name', label=_('Name'))
    schema_id = attrs.TextAttr('schema_id', label=_('Schema ID'))
    status = attrs.TemplatedAttr('status', label=_('Status'), template_name='netbox_branching/attrs/status.html')
    owner = attrs.RelatedObjectAttr('owner', label=_('Owner'), linkify=True)
    description = attrs.TextAttr('description', label=_('Description'))
    last_sync = attrs.TemplatedAttr(
        'last_sync', label=_('Last synced'), template_name='netbox_branching/attrs/last_sync.html'
    )
    latest_change = LatestChangeAttr()
    conflicts = ConflictsAttr()
    schema_name = attrs.TextAttr('schema_name', label=_('Database schema'), style='font-monospace')


class BranchEventHistoryPanel(panels.ObjectPanel):
    template_name = 'netbox_branching/panels/event_history.html'
    title = _('Event History')


class BranchChangeStatsPanel(panels.ObjectPanel):
    """
    Counts of objects created, updated, or deleted within a branch, by object type.

    Parameters:
        action: The ObjectChange action to summarize
    """

    template_name = 'netbox_branching/panels/change_stats.html'

    ACTIONS = {
        ObjectChangeActionChoices.ACTION_CREATE: (_('Created'), 'green'),
        ObjectChangeActionChoices.ACTION_UPDATE: (_('Updated'), 'yellow'),
        ObjectChangeActionChoices.ACTION_DELETE: (_('Deleted'), 'red'),
    }

    def __init__(self, action, **kwargs):
        if action not in self.ACTIONS:
            raise ValueError(f'Invalid action: {action}')
        self.action = action
        title, self.color = self.ACTIONS[action]
        kwargs.setdefault('title', title)
        super().__init__(**kwargs)

    def get_context(self, context):
        ctx = super().get_context(context)
        branch = ctx['object']
        counts = {}
        if branch.ready or branch.merged:
            qs = (
                branch.get_changes()
                .filter(action=self.action)
                .values_list('changed_object_type')
                .annotate(count=Count('pk'))
                .order_by('changed_object_type')
            )
            counts = {ContentType.objects.get_for_id(ct_id): count for ct_id, count in qs}
        return {
            **ctx,
            'action': self.action,
            'color': self.color,
            'counts': counts,
        }


#
# ChangeDiff panels
#


class ChangeDiffPanel(panels.ObjectAttributesPanel):
    branch = attrs.RelatedObjectAttr('branch', label=_('Branch'), linkify=True)
    object_type = attrs.TextAttr('object_type', label=_('Object type'))
    object = attrs.TemplatedAttr(
        'object_repr', label=_('Object'), template_name='netbox_branching/attrs/changediff_object.html'
    )
    action = attrs.ChoiceAttr('action', label=_('Action'))
    last_updated = attrs.DateTimeAttr('last_updated', label=_('Last updated'))


class ChangeDiffConflictsPanel(panels.ObjectPanel):
    template_name = 'netbox_branching/panels/changediff_conflicts.html'
    title = _('Conflicts')


class ChangeDiffDataPanel(panels.ObjectPanel):
    """
    One of the three object snapshots held by a ChangeDiff, with altered attributes highlighted.

    Parameters:
        field: The snapshot to render ("original", "modified", or "current")
        altered: The context key holding the set of altered attribute names
        style: CSS class applied to altered attributes ("removed" or "added")
        requires: If set, the panel renders only when this context key is not None
    """

    template_name = 'netbox_branching/panels/changediff_data.html'

    def __init__(self, field, altered, style, requires=None, **kwargs):
        super().__init__(**kwargs)
        self.field = field
        self.altered = altered
        self.style = style
        self.requires = requires

    def get_context(self, context):
        ctx = super().get_context(context)
        obj = ctx['object']
        if self.title is None:
            if self.field == 'modified':
                ctx['title'] = _('Changed in {branch}').format(branch=obj.branch)
            elif self.field == 'current':
                ctx['title'] = _('Changed in main')
            else:
                ctx['title'] = _('Original')
        return {
            **ctx,
            'data': getattr(obj, self.field),
            'altered': context.get(self.altered) or set(),
            'style': self.style,
            'conflicts': obj.conflicts if self.style == 'added' else None,
            'required': context.get(self.requires) if self.requires else True,
        }

    def should_render(self, context):
        return context['required'] is not None


class ChangeDiffDifferencePanel(panels.ObjectPanel):
    """
    The attributes removed and added between two ChangeDiff snapshots.

    Parameters:
        removed: The context key holding the removed attributes
        added: The context key holding the added attributes
        requires: If set, the panel renders only when this context key is not None
    """

    template_name = 'netbox_branching/panels/changediff_difference.html'
    title = _('Difference')

    def __init__(self, removed, added, requires=None, **kwargs):
        super().__init__(**kwargs)
        self.removed = removed
        self.added = added
        self.requires = requires

    def get_context(self, context):
        return {
            **super().get_context(context),
            'removed': context.get(self.removed),
            'added': context.get(self.added),
            'required': context.get(self.requires) if self.requires else True,
        }

    def should_render(self, context):
        return context['required'] is not None


#
# Merge report panels
#


class MergeReportPanel(panels.ObjectPanel):
    """
    Base class for panels which render the outcome of a branch's most recent merge job.
    """

    def get_context(self, context):
        return {
            **super().get_context(context),
            'last_job': context.get('last_job'),
            'merge_strategy': context.get('merge_strategy'),
            'report_entries': context.get('report_entries') or [],
            'changes_summary': context.get('changes_summary'),
        }


class MergeReportSummaryPanel(MergeReportPanel):
    template_name = 'netbox_branching/panels/merge_report_summary.html'
    title = _('Merge Report')


class MergeReportFailuresPanel(MergeReportPanel):
    template_name = 'netbox_branching/panels/merge_report_failures.html'
    title = _('Merge Failures')

    def should_render(self, context):
        return bool(context['report_entries'])


class MergeReportRecommendationsPanel(MergeReportPanel):
    template_name = 'netbox_branching/panels/merge_report_recommendations.html'
    title = _('Recommendations')

    def should_render(self, context):
        return bool(context['report_entries'])


class MergeReportLogPanel(MergeReportPanel):
    template_name = 'netbox_branching/panels/merge_report_log.html'
    title = _('Log')

    def get_context(self, context):
        ctx = super().get_context(context)
        last_job = ctx['last_job']
        return {
            **ctx,
            'log': (last_job.data or {}).get('log') if last_job else None,
        }

    def should_render(self, context):
        return bool(context['log'])
