from django.core.exceptions import PermissionDenied
from django.utils.module_loading import import_string
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.routers import APIRootView
from rest_framework.viewsets import ModelViewSet

from core.api.serializers import JobSerializer
from core.models import Job
from netbox.api.viewsets import NetBoxReadOnlyModelViewSet
from netbox_branching import filtersets
from netbox_branching.models import ChangeDiff, Branch
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

        serializer = serializers.CommitSerializer(data=request.data)
        commit = serializer.validated_data['commit'] if serializer.is_valid() else False

        # Enqueue a background job
        job = Job.enqueue(
            import_string('netbox_branching.jobs.sync_branch'),
            instance=self.get_object(),
            name='Sync branch',
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

        serializer = serializers.CommitSerializer(data=request.data)
        commit = serializer.validated_data['commit'] if serializer.is_valid() else False

        # Enqueue a background job
        job = Job.enqueue(
            import_string('netbox_branching.jobs.merge_branch'),
            instance=self.get_object(),
            name='Merge branch',
            commit=commit
        )

        return Response(JobSerializer(job, context={'request': request}).data)


class ChangeDiffViewSet(NetBoxReadOnlyModelViewSet):
    queryset = ChangeDiff.objects.all()
    serializer_class = serializers.ChangeDiffSerializer
    filterset_class = filtersets.ChangeDiffFilterSet
