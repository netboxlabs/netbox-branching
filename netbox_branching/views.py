from collections import defaultdict

from core.choices import JobStatusChoices, ObjectChangeActionChoices
from core.filtersets import ObjectChangeFilterSet
from core.models import ObjectChange
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _
from netbox.plugins import get_plugin_config
from netbox.views import generic
from netbox.views.generic.base import BaseMultiObjectView
from utilities.views import GetReturnURLMixin, ViewTab, register_model_view

from . import filtersets, forms, tables
from .choices import BranchStatusChoices
from .constants import QUERY_PARAM
from .error_report import get_entry_message, get_merge_recommendations
from .jobs import MergeBranchJob, MigrateBranchJob, RevertBranchJob, SyncBranchJob
from .models import Branch, ChangeDiff
from .object_actions import BulkMigrate
from .utilities import resolve_changes_summary

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
    actions = (*generic.ObjectListView.actions, BulkMigrate)


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
            last_merge_job = instance.jobs.filter(name=MergeBranchJob.Meta.name).order_by('created').last()
        else:
            stats = {}
            latest_change = None
            last_job = None
            last_merge_job = None

        return {
            'stats': stats,
            'latest_change': latest_change,
            'last_job': last_job,
            'last_job_errored': last_job is not None and last_job.status == JobStatusChoices.STATUS_ERRORED,
            'last_merge_job': last_merge_job,
            'last_merge_job_errored': (
                last_merge_job is not None
                and last_merge_job == last_job
                and last_merge_job.status == JobStatusChoices.STATUS_ERRORED
            ),
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
    actions = {}  # noqa: RUF012
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
    actions = {}  # noqa: RUF012
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
    actions = {}  # noqa: RUF012
    tab = ViewTab(
        label=_('Changes Ahead'),
        badge=lambda obj: obj.get_unmerged_changes().count(),
        permission='netbox_branching.view_branch'
    )

    def get_children(self, request, parent):
        return parent.get_unmerged_changes().order_by('time')


@register_model_view(Branch, 'job-report')
class BranchJobReportView(generic.ObjectView):
    queryset = Branch.objects.all()
    template_name = 'netbox_branching/branch_job_report.html'

    def _build_report_entries(self, instance, last_job, merge_strategy):
        """Resolve each raw report entry into a display-ready dict with message, recommendations, and object info."""
        entries = []
        for entry in last_job.data.get('report', []):
            object_url = None
            object_str = None
            ct_id = entry.get('content_type_id')
            obj_id = entry.get('object_id')
            if ct_id and obj_id:
                try:
                    ct = ContentType.objects.get_for_id(ct_id)
                    try:
                        obj = ct.get_object_for_this_type(pk=obj_id)
                    except ObjectDoesNotExist:
                        # Object may only exist in the branch schema (e.g. created in branch, conflicts on merge)
                        obj = ct.model_class()._default_manager.using(instance.connection_name).get(pk=obj_id)
                    if hasattr(obj, 'get_absolute_url'):
                        object_url = f'{obj.get_absolute_url()}?{QUERY_PARAM}={instance.schema_id}'
                    object_str = str(obj)
                    if not entry.get('value') and (field := entry.get('field')):
                        value = getattr(obj, field, None)
                    else:
                        value = entry.get('value')
                except (ContentType.DoesNotExist, ObjectDoesNotExist):
                    object_str = f'#{obj_id}'
                    value = entry.get('value')
            else:
                value = entry.get('value')
            entries.append({
                **entry,
                'value': value,
                'message': get_entry_message(entry),
                'recommendations': get_merge_recommendations(entry, merge_strategy=merge_strategy),
                'object_url': object_url,
                'object_str': object_str,
            })
        return entries

    def get_extra_context(self, request, instance):
        last_job = instance.jobs.filter(name=MergeBranchJob.Meta.name).order_by('created').last()
        job_data = last_job.data if last_job and last_job.data else {}
        merge_strategy = job_data.get('merge_strategy')
        report_entries = self._build_report_entries(instance, last_job, merge_strategy) if last_job and job_data else []
        stored = job_data.get('changes_summary')
        changes_summary = resolve_changes_summary(stored) if stored else None
        has_unsynced_changes = bool(job_data.get('has_unsynced_changes', False))
        return {
            'last_job': last_job,
            'merge_strategy': merge_strategy,
            'report_entries': report_entries,
            'changes_summary': changes_summary,
            'has_unsynced_changes': has_unsynced_changes,
        }


def _get_change_count(obj):
    return obj.get_unmerged_changes().count()


@register_model_view(Branch, 'changes-merged')
class BranchChangesMergedView(generic.ObjectChildrenView):
    queryset = Branch.objects.all()
    child_model = ObjectChange
    filterset = ObjectChangeFilterSet
    table = tables.ChangesTable
    actions = {}  # noqa: RUF012
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

    @staticmethod
    def _get_changes_summary(changes_qs):
        """
        Compute a deduplicated summary of creates, updates, and deletes from a changes queryset.
        Rules:
          - create + update = create
          - anything + delete = delete
        Returns dict with 'creates', 'updates', 'deletes' (each {ContentType: count}) and totals.
        """
        changes = changes_qs.values('action', 'changed_object_type_id', 'changed_object_id')

        object_actions = defaultdict(set)
        for change in changes:
            key = (change['changed_object_type_id'], change['changed_object_id'])
            object_actions[key].add(change['action'])

        creates = defaultdict(int)
        updates = defaultdict(int)
        deletes = defaultdict(int)

        for (ct_id, _obj_id), actions in object_actions.items():
            if ObjectChangeActionChoices.ACTION_DELETE in actions:
                deletes[ct_id] += 1
            elif ObjectChangeActionChoices.ACTION_CREATE in actions:
                creates[ct_id] += 1
            else:
                updates[ct_id] += 1

        def resolve(counts_dict):
            ct_map = {ct.pk: ct for ct in ContentType.objects.filter(pk__in=counts_dict)}
            return dict(sorted(
                {ct_map[ct_id]: count for ct_id, count in counts_dict.items()}.items(),
                key=lambda item: item[0].model
            ))

        return {
            'creates': resolve(creates),
            'creates_total': sum(creates.values()),
            'updates': resolve(updates),
            'updates_total': sum(updates.values()),
            'deletes': resolve(deletes),
            'deletes_total': sum(deletes.values()),
        }

    def get_action_summary(self, branch):
        return None

    def do_action(self, branch, request, form):
        raise NotImplementedError(f"{self.__class__} must implement action() method.")

    def _build_context(self, branch, form, action_permitted):
        return {
            'branch': branch,
            'action': _('%s Branch') % self.action.title(),
            'action_name': self.action,
            'form': form,
            'action_permitted': action_permitted,
            'conflicts_table': self._get_conflicts_table(branch),
            'changes_summary': self.get_action_summary(branch),
        }

    def get(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        action_permitted = getattr(branch, f'can_{self.action}')
        form = self.form(branch, allow_commit=action_permitted)

        return render(request, self.template_name, self._build_context(branch, form, action_permitted))

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

        return render(request, self.template_name, self._build_context(branch, form, action_permitted))


@register_model_view(Branch, 'sync')
class BranchSyncView(BaseBranchActionView):
    action = 'sync'
    form = forms.BranchSyncForm

    def get_action_summary(self, branch):
        return self._get_changes_summary(branch.get_unsynced_changes())

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

    def get_action_summary(self, branch):
        return self._get_changes_summary(branch.get_unmerged_changes())

    def do_action(self, branch, request, form):
        # Save the merge_strategy setting to the branch
        branch.merge_strategy = form.cleaned_data.get('merge_strategy')
        branch.save()

        # Enqueue a background job to merge the Branch
        MergeBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=form.cleaned_data['commit'],
            job_timeout=get_plugin_config('netbox_branching', 'job_timeout')
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
        return None

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
        action_permitted = branch.can_migrate
        form = self.form()

        return render(request, self.template_name, {
            'branch': branch,
            'form': form,
            'action_permitted': action_permitted,
        })

    def post(self, request, **kwargs):
        branch = self.get_object(**kwargs)
        action_permitted = branch.can_migrate
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


class BranchBulkMigrateView(GetReturnURLMixin, BaseMultiObjectView):
    queryset = Branch.objects.all()
    table = tables.BranchTable
    template_name = 'netbox_branching/branch_bulk_migrate.html'

    def get_required_permission(self):
        return 'netbox_branching.migrate_branch'

    def get(self, request):
        return redirect(self.get_return_url(request))

    def post(self, request):
        if '_confirm' in request.POST:
            form = forms.BulkMigrateBranchForm(request.POST)
            if form.is_valid():
                branches = [
                    branch for branch in form.cleaned_data['pk']
                    if branch.status == BranchStatusChoices.PENDING_MIGRATIONS and branch.can_migrate
                ]
                skipped = len(form.cleaned_data['pk']) - len(branches)
                count = len(branches)
                for branch in branches:
                    MigrateBranchJob.enqueue(instance=branch, user=request.user)
                if count:
                    messages.success(
                        request,
                        _('Queued migration jobs for {count} branch(es).').format(count=count)
                    )
                if skipped:
                    messages.warning(
                        request,
                        _('Skipped {skipped} branch(es) that cannot be migrated.').format(skipped=skipped)
                    )
            return redirect(self.get_return_url(request))

        # Show confirmation page — validate PKs through the form, filter to pending branches
        form = forms.BulkMigrateBranchForm(request.POST)
        if not form.is_valid():
            return redirect(self.get_return_url(request))

        branches = [
            branch for branch in form.cleaned_data['pk']
            if branch.status == BranchStatusChoices.PENDING_MIGRATIONS
        ]
        table = self.table(branches, orderable=False)

        if not table.rows:
            messages.warning(request, _('No branches with pending migrations were selected.'))
            return redirect(self.get_return_url(request))

        form = forms.BulkMigrateBranchForm(initial={'pk': [b.pk for b in branches]})

        return render(request, self.template_name, {
            'form': form,
            'table': table,
            'return_url': self.get_return_url(request),
        })


#
# Change diffs
#

class ChangeDiffListView(generic.ObjectListView):
    queryset = ChangeDiff.objects.all()
    filterset = filtersets.ChangeDiffFilterSet
    filterset_form = forms.ChangeDiffFilterForm
    table = tables.ChangeDiffTable


@register_model_view(ChangeDiff)
class ChangeDiffView(generic.ObjectView):
    queryset = ChangeDiff.objects.all()

    def get_extra_context(self, request, instance):
        # Safely compute altered field sets only when the required data is present
        altered_in_modified = instance.altered_in_modified if instance.original and instance.modified else set()
        altered_in_current = instance.altered_in_current if instance.original and instance.current else set()
        # Compute branch diff (original → modified)
        if instance.original and instance.modified and altered_in_modified:
            branch_diff_removed = {k: instance.original[k] for k in altered_in_modified}
            branch_diff_added = {k: instance.modified[k] for k in altered_in_modified}
        else:
            branch_diff_removed = branch_diff_added = None

        # Compute main diff (original → current)
        if instance.original and instance.current and altered_in_current:
            main_diff_removed = {k: instance.original[k] for k in altered_in_current}
            main_diff_added = {k: instance.current[k] for k in altered_in_current}
        else:
            main_diff_removed = main_diff_added = None

        return {
            'altered_in_modified': altered_in_modified,
            'altered_in_current': altered_in_current,
            'branch_diff_removed': branch_diff_removed,
            'branch_diff_added': branch_diff_added,
            'main_diff_removed': main_diff_removed,
            'main_diff_added': main_diff_added,
        }
