from netbox.plugins import PluginTemplateExtension

from .choices import BranchStatusChoices
from .contextvars import active_branch
from .models import Branch


class BranchSelector(PluginTemplateExtension):

    def navbar(self):
        return self.render('netbox_branching/inc/branch_selector.html', extra_context={
            'active_branch': active_branch.get(),
            'branches': Branch.objects.exclude(status=BranchStatusChoices.MERGED),
        })


template_extensions = [BranchSelector]
