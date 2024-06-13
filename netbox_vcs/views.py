from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.utils.module_loading import import_string
from django.utils.translation import gettext_lazy as _

from core.models import Job
from extras.choices import ObjectChangeActionChoices
from extras.models import ObjectChange
from netbox.context import current_request
from netbox.views import generic
from utilities.views import ViewTab, register_model_view
from . import filtersets, forms, tables
from .models import ChangeDiff, Context


#
# Contexts
#

class ContextListView(generic.ObjectListView):
    queryset = Context.objects.annotate(
        # Annotate the number of associated ChangeDiffs with conflicts
        conflicts=Count('changediff', filter=Q(changediff__conflicts__isnull=False))
    ).order_by('name')
    filterset = filtersets.ContextFilterSet
    filterset_form = forms.ContextFilterForm
    table = tables.ContextTable


@register_model_view(Context)
class ContextView(generic.ObjectView):
    queryset = Context.objects.all()

    def get_extra_context(self, request, instance):
        qs = instance.get_changes().values_list('changed_object_type').annotate(count=Count('pk'))
        stats = {
            'created': {
                ContentType.objects.get(pk=ct).model_class(): count
                for ct, count in qs.filter(action=ObjectChangeActionChoices.ACTION_CREATE)
            },
            'updated': {
                ContentType.objects.get(pk=ct).model_class(): count
                for ct, count in qs.filter(action=ObjectChangeActionChoices.ACTION_UPDATE)
            },
            'deleted': {
                ContentType.objects.get(pk=ct).model_class(): count
                for ct, count in qs.filter(action=ObjectChangeActionChoices.ACTION_DELETE)
            },
        }

        return {
            'stats': stats,
            'unsynced_changes_count': instance.get_unsynced_changes().count(),
            'conflicts_count': ChangeDiff.objects.filter(context=instance, conflicts__isnull=False).count(),
            'sync_form': forms.SyncContextForm(),
            'apply_form': forms.ApplyContextForm(),
        }


@register_model_view(Context, 'edit')
class ContextEditView(generic.ObjectEditView):
    queryset = Context.objects.all()
    form = forms.ContextForm


@register_model_view(Context, 'delete')
class ContextDeleteView(generic.ObjectDeleteView):
    queryset = Context.objects.all()
    default_return_url = 'plugins:netbox_vcs:context_list'


def _get_diff_count(obj):
    return ChangeDiff.objects.filter(context=obj).count()


@register_model_view(Context, 'diff')
class ContextDiffView(generic.ObjectChildrenView):
    queryset = Context.objects.all()
    child_model = ChangeDiff
    filterset = filtersets.ChangeDiffFilterSet
    table = tables.ChangeDiffTable
    actions = {}
    tab = ViewTab(
        label=_('Diff'),
        badge=_get_diff_count,
        permission='netbox_vcs.view_context'
    )

    def get_children(self, request, parent):
        return ChangeDiff.objects.filter(context=parent)


def _get_change_count(obj):
    return obj.get_changes().count()


@register_model_view(Context, 'replay')
class ContextReplayView(generic.ObjectChildrenView):
    queryset = Context.objects.all()
    child_model = ObjectChange
    table = tables.ReplayTable
    actions = {}
    tab = ViewTab(
        label=_('Replay'),
        badge=_get_change_count,
        permission='netbox_vcs.view_context'
    )

    def get_children(self, request, parent):
        return parent.get_changes().order_by('time')


@register_model_view(Context, 'sync')
class ContextSyncView(generic.ObjectView):
    queryset = Context.objects.all()

    def post(self, request, **kwargs):
        context = self.get_object(**kwargs)
        form = forms.SyncContextForm(request.POST)

        if form.is_valid():
            # Enqueue a background job to sync the Context
            Job.enqueue(
                import_string('netbox_vcs.jobs.sync_context'),
                instance=context,
                name='Sync context',
                commit=form.cleaned_data['commit']
            )
            messages.success(request, f"Syncing of context {context} in progress")

        return redirect(context.get_absolute_url())


@register_model_view(Context, 'apply')
class ContextApplyView(generic.ObjectView):
    queryset = Context.objects.all()

    def post(self, request, **kwargs):
        context = self.get_object(**kwargs)
        form = forms.ApplyContextForm(request.POST)

        if form.is_valid():
            # Enqueue a background job to apply the Context
            Job.enqueue(
                import_string('netbox_vcs.jobs.apply_context'),
                instance=context,
                name='Apply context',
                user=request.user,
                commit=form.cleaned_data['commit'],
                request_id=current_request.get().id
            )
            messages.success(request, f"Application of context {context} in progress")

        return redirect(context.get_absolute_url())


class ContextBulkImportView(generic.BulkImportView):
    queryset = Context.objects.all()
    model_form = forms.ContextImportForm


class ContextBulkEditView(generic.BulkEditView):
    queryset = Context.objects.all()
    filterset = filtersets.ContextFilterSet
    table = tables.ContextTable
    form = forms.ContextBulkEditForm


class ContextBulkDeleteView(generic.BulkDeleteView):
    queryset = Context.objects.all()
    filterset = filtersets.ContextFilterSet
    table = tables.ContextTable


#
# Change diffs
#

class ChangeDiffListView(generic.ObjectListView):
    queryset = ChangeDiff.objects.all()
    filterset = filtersets.ChangeDiffFilterSet
    filterset_form = forms.ChangeDiffFilterForm
    table = tables.ChangeDiffTable
