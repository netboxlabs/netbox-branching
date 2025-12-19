from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from core.filtersets import ObjectChangeFilterSet
from core.models import ObjectChange
from netbox.views import generic
from utilities.views import ViewTab, register_model_view
from . import filtersets, forms, tables
from .choices import BranchStatusChoices
from .jobs import JOB_TIMEOUT, MergeBranchJob, MigrateBranchJob, RevertBranchJob, SyncBranchJob
from .models import Branch, ChangeDiff


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
        if instance.ready or instance.merged:
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
            latest_change = instance.get_changes().order_by('time').last()
            last_job = instance.jobs.order_by('created').last()
        else:
            stats = {}
            latest_change = None
            last_job = None

        return {
            'stats': stats,
            'latest_change': latest_change,
            'last_job': last_job,
            'conflicts_count': ChangeDiff.objects.filter(branch=instance, conflicts__isnull=False).count(),
        }


@register_model_view(Branch, 'edit')
class BranchEditView(generic.ObjectEditView):
    queryset = Branch.objects.all()
    form = forms.BranchForm

    def alter_object(self, obj, request, url_args, url_kwargs):
        if not obj.pk:
            obj.owner = request.user
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


@register_model_view(Branch, 'changes-behind')
class BranchChangesBehindView(generic.ObjectChildrenView):
    queryset = Branch.objects.all()
    child_model = ObjectChange
    filterset = ObjectChangeFilterSet
    table = tables.ChangesTable
    actions = {}
    tab = ViewTab(
        label=_('Changes Behind'),
        badge=lambda obj: obj.get_unsynced_changes().count(),
        permission='netbox_branching.view_branch'
    )

    def get_children(self, request, parent):
        return parent.get_unsynced_changes().order_by('time')


@register_model_view(Branch, 'changes-ahead')
class BranchChangesAheadView(generic.ObjectChildrenView):
    queryset = Branch.objects.all()
    child_model = ObjectChange
    filterset = ObjectChangeFilterSet
    table = tables.ChangesTable
    actions = {}
    tab = ViewTab(
        label=_('Changes Ahead'),
        badge=lambda obj: obj.get_unmerged_changes().count(),
        permission='netbox_branching.view_branch'
    )

    def get_children(self, request, parent):
        return parent.get_unmerged_changes().order_by('time')


def _get_change_count(obj):
    return obj.get_unmerged_changes().count()


@register_model_view(Branch, 'changes-merged')
class BranchChangesMergedView(generic.ObjectChildrenView):
    queryset = Branch.objects.all()
    child_model = ObjectChange
    filterset = ObjectChangeFilterSet
    table = tables.ChangesTable
    actions = {}
    tab = ViewTab(
        label=_('Changes Merged'),
        badge=lambda obj: obj.get_merged_changes().count(),
        permission='netbox_branching.view_branch',
        hide_if_empty=True
    )

    def get_children(self, request, parent):
        return parent.get_merged_changes().order_by('time')


class BaseBranchActionView(generic.ObjectView):
    """
    Base view for syncing or merging a Branch.
    """
    queryset = Branch.objects.all()
    form = None  # Must be set by derived classes
    template_name = 'netbox_branching/branch_action.html'
    action = None
    valid_states = (
        BranchStatusChoices.READY,
    )

    def get_required_permission(self):
        return f'netbox_branching.{self.action}_branch'

    @staticmethod
    def _get_conflicts_table(branch):
        conflicts = ChangeDiff.objects.filter(branch=branch, conflicts__isnull=False)
        conflicts_table = tables.ChangeDiffTable(conflicts)
        conflicts_table.columns.show('pk')

        return conflicts_table

    def do_action(self, branch, request, form):
        raise NotImplementedError(f"{self.__class__} must implement action() method.")

    def get(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        action_permitted = getattr(branch, f'can_{self.action}')
        form = self.form(branch, allow_commit=action_permitted)

        return render(request, self.template_name, {
            'branch': branch,
            'action': _(f'{self.action.title()} Branch'),
            'form': form,
            'action_permitted': action_permitted,
            'conflicts_table': self._get_conflicts_table(branch),
        })

    def post(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        action_permitted = getattr(branch, f'can_{self.action}')
        form = self.form(branch, request.POST, allow_commit=action_permitted)

        if branch.status not in self.valid_states:
            messages.error(request, _(
                "The branch must be in one of the following states to perform this action: {valid_states}"
            ).format(valid_states=', '.join(self.valid_states)))
        elif form.is_valid():
            return self.do_action(branch, request, form)

        return render(request, self.template_name, {
            'branch': branch,
            'action': _(f'{self.action.title()} Branch'),
            'form': form,
            'action_permitted': action_permitted,
            'conflicts_table': self._get_conflicts_table(branch),
        })


@register_model_view(Branch, 'sync')
class BranchSyncView(BaseBranchActionView):
    action = 'sync'
    form = forms.BranchSyncForm

    def do_action(self, branch, request, form):
        # Enqueue a background job to sync the Branch
        SyncBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=form.cleaned_data['commit']
        )
        messages.success(request, _("Syncing of branch {branch} in progress").format(branch=branch))

        return redirect(branch.get_absolute_url())


@register_model_view(Branch, 'merge')
class BranchMergeView(BaseBranchActionView):
    action = 'merge'
    form = forms.BranchMergeForm

    def do_action(self, branch, request, form):
        # Save the merge_strategy setting to the branch
        branch.merge_strategy = form.cleaned_data.get('merge_strategy')
        branch.save()

        # Enqueue a background job to merge the Branch
        MergeBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=form.cleaned_data['commit'],
            job_timeout=JOB_TIMEOUT
        )
        messages.success(request, _("Merging of branch {branch} in progress").format(branch=branch))

        return redirect(branch.get_absolute_url())


@register_model_view(Branch, 'revert')
class BranchRevertView(BaseBranchActionView):
    action = 'revert'
    form = forms.BranchRevertForm
    valid_states = (
        BranchStatusChoices.MERGED,
    )

    def do_action(self, branch, request, form):
        # Enqueue a background job to revert the Branch
        RevertBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=form.cleaned_data['commit']
        )
        messages.success(request, _("Reverting branch {branch}").format(branch=branch))

        return redirect(branch.get_absolute_url())


@register_model_view(Branch, 'archive')
class BranchArchiveView(generic.ObjectView):
    """
    Archive a merged Branch, deleting its database schema but retaining the Branch object.
    """
    queryset = Branch.objects.all()
    template_name = 'netbox_branching/branch_archive.html'

    def get_required_permission(self):
        return 'netbox_branching.archive_branch'

    @staticmethod
    def _validate(request, branch):
        if branch.status != BranchStatusChoices.MERGED:
            messages.error(request, _("Only merged branches can be archived."))
            return redirect(branch.get_absolute_url())
        if not branch.can_revert:
            messages.error(request, _("Reverting this branch is disallowed per policy."))
            return redirect(branch.get_absolute_url())

    def get(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        self._validate(request, branch)
        form = forms.ConfirmationForm()

        return render(request, self.template_name, {
            'branch': branch,
            'form': form,
        })

    def post(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        self._validate(request, branch)
        form = forms.ConfirmationForm(request.POST)

        if form.is_valid():
            branch.archive(user=request.user)

            messages.success(request, _("Branch {branch} has been archived.").format(branch=branch))
            return redirect(branch.get_absolute_url())

        return render(request, self.template_name, {
            'branch': branch,
            'form': form,
        })


@register_model_view(Branch, 'migrate')
class BranchMigrateView(generic.ObjectView):
    queryset = Branch.objects.all()
    form = forms.MigrateBranchForm
    template_name = 'netbox_branching/branch_migrate.html'

    def get_required_permission(self):
        return 'netbox_branching.migrate_branch'

    def get(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        action_permitted = getattr(branch, 'can_migrate')
        form = self.form()

        return render(request, self.template_name, {
            'branch': branch,
            'form': form,
            'action_permitted': action_permitted,
        })

    def post(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        action_permitted = getattr(branch, 'can_migrate')
        form = self.form(request.POST)

        if branch.status != BranchStatusChoices.PENDING_MIGRATIONS:
            messages.error(request, _("The branch is not ready to be migrated."))
        elif form.is_valid():
            # Enqueue a background job to migrate the Branch
            MigrateBranchJob.enqueue(instance=branch, user=request.user)
            messages.success(request, _("Migration of branch {branch} in progress").format(branch=branch))
            return redirect(branch.get_absolute_url())

        return render(request, self.template_name, {
            'branch': branch,
            'form': form,
            'action_permitted': action_permitted,
        })


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
