from netbox.plugins import PluginTemplateExtension

from .models import Context
from .utilities import get_active_context


class ContextSelector(PluginTemplateExtension):

    def navbar(self):
        return self.render('netbox_vcs/inc/context_selector.html', extra_context={
            'active_context': get_active_context(),
            'contexts': Context.objects.all(),
        })


template_extensions = [ContextSelector]
