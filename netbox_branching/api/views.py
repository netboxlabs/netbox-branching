from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.routers import APIRootView
from rest_framework.viewsets import ModelViewSet

from core.api.serializers import JobSerializer
from netbox.api.viewsets import BaseViewSet, NetBoxReadOnlyModelViewSet
from netbox_branching import filtersets
from netbox_branching.jobs import MergeBranchJob, SyncBranchJob
from netbox_branching.models import Branch, BranchEvent, ChangeDiff
from . import serializers


class RootView(APIRootView):
    def get_view_name(self):
        return 'Branching'


class BranchViewSet(ModelViewSet):
    queryset = Branch.objects.all()
    serializer_class = serializers.BranchSerializer
    filterset_class = filtersets.BranchFilterSet

    @action(detail=True, methods=['post'])
    def sync(self, request, pk):
        """
        Enqueue a background job to run Branch.sync().
        """
        if not request.user.has_perm('netbox_branching.sync_branch'):
            raise PermissionDenied("This user does not have permission to sync branches.")

        branch = self.get_object()
        if not branch.ready:
            return HttpResponseBadRequest("Branch is not ready to sync")

        serializer = serializers.CommitSerializer(data=request.data)
        commit = serializer.validated_data['commit'] if serializer.is_valid() else False

        # Enqueue a background job
        job = SyncBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=commit
        )

        return Response(JobSerializer(job, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def merge(self, request, pk):
        """
        Enqueue a background job to run Branch.merge().
        """
        if not request.user.has_perm('netbox_branching.merge_branch'):
            raise PermissionDenied("This user does not have permission to merge branches.")

        branch = self.get_object()
        if not branch.ready:
            return HttpResponseBadRequest("Branch is not ready to merge")

        serializer = serializers.CommitSerializer(data=request.data)
        commit = serializer.validated_data['commit'] if serializer.is_valid() else False

        # Enqueue a background job
        job = MergeBranchJob.enqueue(
            instance=branch,
            user=request.user,
            commit=commit
        )

        return Response(JobSerializer(job, context={'request': request}).data)


class BranchEventViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    queryset = BranchEvent.objects.all()
    serializer_class = serializers.BranchEventSerializer
    filterset_class = filtersets.BranchEventFilterSet


class ChangeDiffViewSet(NetBoxReadOnlyModelViewSet):
    queryset = ChangeDiff.objects.all()
    serializer_class = serializers.ChangeDiffSerializer
    filterset_class = filtersets.ChangeDiffFilterSet
