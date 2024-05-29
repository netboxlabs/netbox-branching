from netbox.plugins import PluginTemplateExtension

from .contextvars import active_context
from .models import Context


class ContextSelector(PluginTemplateExtension):

    def navbar(self):
        return self.render('netbox_vcs/inc/context_selector.html', extra_context={
            'active_context': active_context.get(),
            'contexts': Context.objects.all(),
        })


template_extensions = [ContextSelector]
