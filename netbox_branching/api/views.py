from typing import ClassVar

from core.api.serializers import JobSerializer
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from drf_spectacular.utils import extend_schema
from netbox.api.authentication import IsAuthenticatedOrLoginNotRequired
from netbox.api.viewsets import BaseViewSet, NetBoxReadOnlyModelViewSet
from netbox.plugins import get_plugin_config
from netbox.api.authentication import IsAuthenticatedOrLoginNotRequired
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.routers import APIRootView
from rest_framework.viewsets import ModelViewSet

from netbox_branching import filtersets
from netbox_branching.jobs import MergeBranchJob, RevertBranchJob, SyncBranchJob
from netbox_branching.models import Branch, BranchEvent, ChangeDiff
from netbox_branching.utilities import get_branchable_object_types

from . import serializers


class RootView(APIRootView):
    def get_view_name(self):
        return 'Branching'


class BranchViewSet(ModelViewSet):
    queryset = Branch.objects.all()
    serializer_class = serializers.BranchSerializer
    filterset_class = filtersets.BranchFilterSet

    def _check_conflicts(self, branch, serializer):
        """
        Return a 409 response if the branch has conflicts and they have not been
        acknowledged, else None.
        """
        if serializer.validated_data.get('acknowledge_conflicts', False):
            return None
        conflicts = ChangeDiff.objects.filter(
            branch=branch, conflicts__isnull=False
        ).select_related('object_type')
        if not conflicts.exists():
            return None
        return Response(
            {
                'detail': 'All conflicts must be acknowledged before this action can proceed.',
                'conflicts': serializers.ConflictSummarySerializer(conflicts, many=True).data,
            },
            status=status.HTTP_409_CONFLICT,
        )

    @extend_schema(
        methods=['post'],
        request=serializers.CommitSerializer(),
        responses={200: JobSerializer(), 409: serializers.ConflictResponseSerializer()},
    )
    @action(detail=True, methods=['post'])
    def sync(self, request, pk):
        """
        Enqueue a background job to synchronize a branch from main.
        """
        if not request.user.has_perm('netbox_branching.sync_branch'):
            raise PermissionDenied("This user does not have permission to sync branches.")

        branch = self.get_object()
        if not branch.ready:
            return HttpResponseBadRequest("Branch is not ready to sync.")

        serializer = serializers.CommitSerializer(data=request.data)
        commit = serializer.validated_data.get('commit', True) if serializer.is_valid() else False

        if conflict_response := self._check_conflicts(branch, serializer):
            return conflict_response

        # Enqueue a background job
        job = SyncBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=commit
        )

        return Response(JobSerializer(job, context={'request': request}).data)

    @extend_schema(
        methods=['post'],
        request=serializers.CommitSerializer(),
        responses={200: JobSerializer(), 409: serializers.ConflictResponseSerializer()},
    )
    @action(detail=True, methods=['post'])
    def merge(self, request, pk):
        """
        Enqueue a background job to merge a branch.
        """
        if not request.user.has_perm('netbox_branching.merge_branch'):
            raise PermissionDenied("This user does not have permission to merge branches.")

        branch = self.get_object()
        if not branch.ready:
            return HttpResponseBadRequest("Branch is not ready to merge.")

        serializer = serializers.CommitSerializer(data=request.data)
        commit = serializer.validated_data.get('commit', True) if serializer.is_valid() else False

        if conflict_response := self._check_conflicts(branch, serializer):
            return conflict_response

        # Enqueue a background job
        job = MergeBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=commit,
            job_timeout=get_plugin_config('netbox_branching', 'job_timeout')
        )

        return Response(JobSerializer(job, context={'request': request}).data)

    @extend_schema(
        methods=['post'],
        request=serializers.CommitSerializer(),
        responses={200: JobSerializer()},
    )
    @action(detail=True, methods=['post'])
    def revert(self, request, pk):
        """
        Enqueue a background job to revert a merged branch.
        """
        if not request.user.has_perm('netbox_branching.revert_branch'):
            raise PermissionDenied("This user does not have permission to revert branches.")

        branch = self.get_object()
        if not branch.merged:
            return HttpResponseBadRequest("Only merged branches can be reverted.")

        serializer = serializers.CommitSerializer(data=request.data)
        commit = serializer.validated_data.get('commit', True) if serializer.is_valid() else False

        # Enqueue a background job
        job = RevertBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=commit
        )

        return Response(JobSerializer(job, context={'request': request}).data)

    @extend_schema(
        methods=['post'],
        responses={200: serializers.BranchSerializer()},
    )
    @action(detail=True, methods=['post'])
    def archive(self, request, pk):
        """
        Archive a merged branch, deprovisioning its schema.
        """
        if not request.user.has_perm('netbox_branching.archive_branch'):
            raise PermissionDenied("This user does not have permission to archive branches.")

        branch = self.get_object()
        if not branch.merged:
            return HttpResponseBadRequest("Only merged branches can be archived.")
        if not branch.can_archive:
            return HttpResponseBadRequest("Archiving this branch is not permitted.")

        branch.archive(user=request.user)
        branch.refresh_from_db()

        serializer = self.get_serializer(branch)
        return Response(serializer.data)


class BranchEventViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    queryset = BranchEvent.objects.all()
    serializer_class = serializers.BranchEventSerializer
    filterset_class = filtersets.BranchEventFilterSet


class ChangeDiffViewSet(NetBoxReadOnlyModelViewSet):
    queryset = ChangeDiff.objects.all()
    serializer_class = serializers.ChangeDiffSerializer
    filterset_class = filtersets.ChangeDiffFilterSet


class BranchableModelViewSet(NetBoxReadOnlyModelViewSet):
    """
    List all models that support branching, including models from custom plugins.
    """
    permission_classes: ClassVar = [IsAuthenticatedOrLoginNotRequired]

    def list(self, request):
        data = []
        for ot in get_branchable_object_types().order_by('app_label', 'model'):
            entry = {
                'app_label': ot.app_label,
                'model': ot.model,
                'verbose_name': None,
                'verbose_name_plural': None,
            }
            if model_class := ot.model_class():
                entry['verbose_name'] = model_class._meta.verbose_name
                entry['verbose_name_plural'] = model_class._meta.verbose_name_plural
            data.append(entry)

        serializer = serializers.BranchableModelSerializer(data, many=True)
        return Response(serializer.data)
