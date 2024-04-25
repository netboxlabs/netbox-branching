from django.contrib import messages
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _

from netbox.views import generic
from utilities.exceptions import AbortTransaction
from utilities.views import ViewTab, register_model_view

from . import forms, tables
from .models import Context, ObjectChange


class ContextListView(generic.ObjectListView):
    queryset = Context.objects.all()
    # filterset = filtersets.ContextFilterSet
    # filterset_form = forms.ContextFilterForm
    table = tables.ContextTable


@register_model_view(Context)
class ContextView(generic.ObjectView):
    queryset = Context.objects.all()

    def get_extra_context(self, request, instance):
        return {
            'apply_form': forms.ApplyContextForm()
        }


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
        permission='netbox_vcs.view_context'
    )

    def get_extra_context(self, request, instance):
        return {
            'diff': instance.diff()
        }


def _get_change_count(obj):
    return ObjectChange.objects.using(f'schema_{obj.schema_name}').count()


@register_model_view(Context, 'replay')
class ContextReplayView(generic.ObjectView):
    queryset = Context.objects.all()
    template_name = 'netbox_vcs/context_replay.html'
    tab = ViewTab(
        label=_('Replay'),
        badge=_get_change_count,
        permission='netbox_vcs.view_context'
    )

    def get_extra_context(self, request, instance):
        replay = []
        for change in ObjectChange.objects.using(f'schema_{instance.schema_name}').order_by('time'):
            replay.append({
                'model': change.changed_object_type.model_class(),
                'change': change,
                'data': change.diff(),
            })

        return {
            'replay': replay
        }


@register_model_view(Context, 'apply')
class ContextReplayView(generic.ObjectView):
    queryset = Context.objects.all()

    def post(self, request, **kwargs):
        context = self.get_object(**kwargs)
        form = forms.ApplyContextForm(request.POST)

        if form.is_valid():
            try:
                context.apply(form.cleaned_data['commit'])
                messages.success(request, f"Applied context {context}!")
                context.delete()
                return redirect('plugins:netbox_vcs:context_list')
            except AbortTransaction:
                messages.info(request, f"Applied & rolled back context {context}")

        return redirect(context.get_absolute_url())
