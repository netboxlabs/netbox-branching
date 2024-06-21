from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.utils.module_loading import import_string
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from core.filtersets import ObjectChangeFilterSet
from core.models import Job, ObjectChange
from netbox.views import generic
from utilities.views import ViewTab, register_model_view
from . import filtersets, forms, tables
from .models import ChangeDiff, Branch


#
# Branches
#

class BranchListView(generic.ObjectListView):
    queryset = Branch.objects.annotate(
        # Annotate the number of associated ChangeDiffs with conflicts
        conflicts=Count('changediff', filter=Q(changediff__conflicts__isnull=False))
    ).order_by('name')
    filterset = filtersets.BranchFilterSet
    filterset_form = forms.BranchFilterForm
    table = tables.BranchTable


@register_model_view(Branch)
class BranchView(generic.ObjectView):
    queryset = Branch.objects.all()

    def get_extra_context(self, request, instance):
        qs = instance.get_changes().values_list('changed_object_type').annotate(count=Count('pk'))
        if instance.ready:
            stats = {
                'created': {
                    ContentType.objects.get(pk=ct): count
                    for ct, count in qs.filter(action=ObjectChangeActionChoices.ACTION_CREATE)
                },
                'updated': {
                    ContentType.objects.get(pk=ct): count
                    for ct, count in qs.filter(action=ObjectChangeActionChoices.ACTION_UPDATE)
                },
                'deleted': {
                    ContentType.objects.get(pk=ct): count
                    for ct, count in qs.filter(action=ObjectChangeActionChoices.ACTION_DELETE)
                },
            }
        else:
            stats = {}

        return {
            'stats': stats,
            'unsynced_changes_count': instance.get_unsynced_changes().count(),
            'conflicts_count': ChangeDiff.objects.filter(branch=instance, conflicts__isnull=False).count(),
            'sync_form': forms.SyncBranchForm(),
            'merge_form': forms.MergeBranchForm(),
        }


@register_model_view(Branch, 'edit')
class BranchEditView(generic.ObjectEditView):
    queryset = Branch.objects.all()
    form = forms.BranchForm

    def alter_object(self, obj, request, url_args, url_kwargs):
        if not obj.pk:
            obj.user = request.user
        return obj


@register_model_view(Branch, 'delete')
class BranchDeleteView(generic.ObjectDeleteView):
    queryset = Branch.objects.all()
    default_return_url = 'plugins:netbox_branching:branch_list'


def _get_diff_count(obj):
    return ChangeDiff.objects.filter(branch=obj).count()


@register_model_view(Branch, 'diff')
class BranchDiffView(generic.ObjectChildrenView):
    queryset = Branch.objects.all()
    child_model = ChangeDiff
    filterset = filtersets.ChangeDiffFilterSet
    table = tables.ChangeDiffTable
    actions = {}
    tab = ViewTab(
        label=_('Diff'),
        badge=_get_diff_count,
        permission='netbox_branching.view_branch'
    )

    def get_children(self, request, parent):
        return ChangeDiff.objects.filter(branch=parent)


def _get_change_count(obj):
    return obj.get_changes().count()


@register_model_view(Branch, 'changes')
class BranchChangesView(generic.ObjectChildrenView):
    queryset = Branch.objects.all()
    child_model = ObjectChange
    filterset = ObjectChangeFilterSet
    table = tables.ChangesTable
    actions = {}
    tab = ViewTab(
        label=_('Changes'),
        badge=_get_change_count,
        permission='netbox_branching.view_branch'
    )

    def get_children(self, request, parent):
        return parent.get_changes().order_by('time')


@register_model_view(Branch, 'sync')
class BranchSyncView(generic.ObjectView):
    queryset = Branch.objects.all()

    def post(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        form = forms.SyncBranchForm(request.POST)

        if form.is_valid():
            # Enqueue a background job to sync the Branch
            Job.enqueue(
                import_string('netbox_branching.jobs.sync_branch'),
                instance=branch,
                name='Sync branch',
                commit=form.cleaned_data['commit']
            )
            messages.success(request, f"Syncing of branch {branch} in progress")

        return redirect(branch.get_absolute_url())


@register_model_view(Branch, 'merge')
class BranchMergeView(generic.ObjectView):
    queryset = Branch.objects.all()

    def post(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        form = forms.MergeBranchForm(request.POST)

        if form.is_valid():
            # Enqueue a background job to merge the Branch
            Job.enqueue(
                import_string('netbox_branching.jobs.merge_branch'),
                instance=branch,
                name='Merge branch',
                user=request.user,
                commit=form.cleaned_data['commit']
            )
            messages.success(request, f"Merging of branch {branch} in progress")

        return redirect(branch.get_absolute_url())


class BranchBulkImportView(generic.BulkImportView):
    queryset = Branch.objects.all()
    model_form = forms.BranchImportForm


class BranchBulkEditView(generic.BulkEditView):
    queryset = Branch.objects.all()
    filterset = filtersets.BranchFilterSet
    table = tables.BranchTable
    form = forms.BranchBulkEditForm


class BranchBulkDeleteView(generic.BulkDeleteView):
    queryset = Branch.objects.all()
    filterset = filtersets.BranchFilterSet
    table = tables.BranchTable


#
# Change diffs
#

class ChangeDiffListView(generic.ObjectListView):
    queryset = ChangeDiff.objects.all()
    filterset = filtersets.ChangeDiffFilterSet
    filterset_form = forms.ChangeDiffFilterForm
    table = tables.ChangeDiffTable
