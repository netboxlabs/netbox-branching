from rest_framework.routers import APIRootView
from rest_framework.viewsets import ModelViewSet

from netbox.api.viewsets import NetBoxReadOnlyModelViewSet
from netbox_vcs import filtersets
from netbox_vcs.models import ChangeDiff, Branch
from . import serializers


class VCSRootView(APIRootView):
    def get_view_name(self):
        return 'VCS'


class BranchViewSet(ModelViewSet):
    queryset = Branch.objects.all()
    serializer_class = serializers.BranchSerializer
    filterset_class = filtersets.BranchFilterSet


class ChangeDiffViewSet(NetBoxReadOnlyModelViewSet):
    queryset = ChangeDiff.objects.all()
    serializer_class = serializers.ChangeDiffSerializer
    filterset_class = filtersets.ChangeDiffFilterSet
