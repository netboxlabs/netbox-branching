from django.contrib.contenttypes.models import ContentType

from netbox.plugins import PluginTemplateExtension, get_plugin_config
from .choices import BranchStatusChoices
from .contextvars import active_branch
from .models import Branch, ChangeDiff

__all__ = (
    'BranchNotification',
    'BranchSelector',
    'BranchWarning',
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


class BranchWarning(PluginTemplateExtension):

    def alerts(self):
        # Warn the use if the branch being displayed or the active branch has a job timeout that is extremely long
        if (job_timeout_warning := get_plugin_config("netbox_branching", "job_timeout_warning")) is None:
            # If the platform owner wants to suppress the warning return nothing
            return ''
        if isinstance(self.context['object'], Branch):
            # The current view is a Branch, Let see what the job timeout is
            job_timeout = self.context['object'].job_timeout
            job_timeout_minutes = job_timeout // 60
        else:
            job_timeout = None
            job_timeout_minutes = None

        active_job_timeout = None
        active_job_timeout_minutes = None
        if hasattr(active_branch.get(), 'job_timeout'):
            # There is an Active Branch, Let see what the job timeout is
            if not (hasattr(self.context['object'], 'id') and active_branch.get().id == self.context['object'].id):
                # The current view is not the active branch, so we can show
                # the active branch job timeout waning too
                active_job_timeout = active_branch.get().job_timeout
                active_job_timeout_minutes = active_job_timeout // 60
        if (
                (job_timeout is not None and job_timeout > job_timeout_warning)
                or
                (active_job_timeout is not None and active_job_timeout > job_timeout_warning)
            ):
            return self.render('netbox_branching/inc/branch_warning.html', extra_context={
                'active_job_timeout': active_job_timeout,
                'active_job_timeout_minutes': active_job_timeout_minutes,
                'job_timeout': job_timeout,
                'job_timeout_minutes': job_timeout_minutes,
                'job_timeout_warning': job_timeout_warning,
            })
        # Nether the active branch nor the branch being displayed has a job timeout that is too long
        return ''


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
    BranchWarning,
    ScriptNotification,
    ShareButton,
)
