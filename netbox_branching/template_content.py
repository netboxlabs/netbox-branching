from django.contrib.contenttypes.models import ContentType

from netbox.plugins import PluginTemplateExtension
from .choices import BranchStatusChoices
from .contextvars import active_branch
from .models import Branch, ChangeDiff

__all__ = (
    'BranchNotification',
    'BranchSelector',
    'ScriptNotification',
    'ShareButton',
    'template_extensions',
)


class BranchSelector(PluginTemplateExtension):

    def navbar(self):
        return self.render('netbox_branching/inc/branch_selector.html', extra_context={
            'active_branch': active_branch.get(),
            'branches': Branch.objects.filter(status__in=BranchStatusChoices.WORKING),
        })


class ShareButton(PluginTemplateExtension):

    def buttons(self):
        return self.render('netbox_branching/inc/share_button.html', extra_context={
            'active_branch': active_branch.get(),
        })


class BranchNotification(PluginTemplateExtension):

    def alerts(self):
        if not (instance := self.context['object']):
            return ''

        ct = ContentType.objects.get_for_model(instance)
        relevant_changes = ChangeDiff.objects.filter(
            object_type=ct,
            object_id=instance.pk
        ).exclude(
            branch__status=BranchStatusChoices.MERGED
        ).exclude(
            branch=active_branch.get()
        )
        branches = [
            diff.branch for diff in relevant_changes.only('branch')
        ]
        return self.render('netbox_branching/inc/modified_notice.html', extra_context={
            'branches': branches,
        })


class ScriptNotification(PluginTemplateExtension):
    models = ['extras.script']

    def alerts(self):
        return self.render('netbox_branching/inc/script_alert.html', extra_context={
            'active_branch': active_branch.get(),
        })


template_extensions = (
    BranchSelector,
    BranchNotification,
    ScriptNotification,
    ShareButton,
)
