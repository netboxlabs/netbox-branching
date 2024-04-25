from django.utils.translation import gettext_lazy as _

from netbox.views import generic
from utilities.views import ViewTab, register_model_view

from . import forms, tables
from .models import Context


class ContextListView(generic.ObjectListView):
    queryset = Context.objects.all()
    # filterset = filtersets.ContextFilterSet
    # filterset_form = forms.ContextFilterForm
    table = tables.ContextTable


@register_model_view(Context)
class ContextView(generic.ObjectView):
    queryset = Context.objects.all()


@register_model_view(Context, 'edit')
class ContextEditView(generic.ObjectEditView):
    queryset = Context.objects.all()
    form = forms.ContextForm


@register_model_view(Context, 'delete')
class ContextDeleteView(generic.ObjectDeleteView):
    queryset = Context.objects.all()
    default_return_url = 'plugins:netbox_vcs:context_list'


@register_model_view(Context, 'diff')
class ContextDiffView(generic.ObjectView):
    queryset = Context.objects.all()
    template_name = 'netbox_vcs/context_diff.html'
    tab = ViewTab(
        label=_('Diff'),
        # badge=lambda obj: Stuff.objects.filter(site=obj).count(),
        permission='netbox_vcs.view_context'
    )

    def get_extra_context(self, request, instance):
        return {
            'diff': instance.diff()
        }
