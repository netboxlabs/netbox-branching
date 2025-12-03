from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.routers import APIRootView
from rest_framework.viewsets import ModelViewSet

from core.api.serializers import JobSerializer
from netbox.api.viewsets import BaseViewSet, NetBoxReadOnlyModelViewSet
from netbox_branching import filtersets
from netbox_branching.jobs import JOB_TIMEOUT, MergeBranchJob, RevertBranchJob, SyncBranchJob
from netbox_branching.models import Branch, BranchEvent, ChangeDiff
from . import serializers


class RootView(APIRootView):
    def get_view_name(self):
        return 'Branching'


class BranchViewSet(ModelViewSet):
    queryset = Branch.objects.all()
    serializer_class = serializers.BranchSerializer
    filterset_class = filtersets.BranchFilterSet

    @extend_schema(
        methods=['post'],
        request=serializers.CommitSerializer(),
        responses={200: JobSerializer()},
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
        commit = serializer.validated_data['commit'] if serializer.is_valid() else False

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
        responses={200: JobSerializer()},
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
        commit = serializer.validated_data['commit'] if serializer.is_valid() else False

        # Enqueue a background job
        job = MergeBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=commit,
            job_timeout=JOB_TIMEOUT
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
        commit = serializer.validated_data['commit'] if serializer.is_valid() else False

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
