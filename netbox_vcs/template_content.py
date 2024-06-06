from netbox.plugins import PluginTemplateExtension

from .choices import ContextStatusChoices
from .contextvars import active_context
from .models import Context


class ContextSelector(PluginTemplateExtension):

    def navbar(self):
        return self.render('netbox_vcs/inc/context_selector.html', extra_context={
            'active_context': active_context.get(),
            'contexts': Context.objects.exclude(status=ContextStatusChoices.APPLIED),
        })


template_extensions = [ContextSelector]
