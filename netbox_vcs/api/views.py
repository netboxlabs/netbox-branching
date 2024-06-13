from rest_framework.routers import APIRootView
from rest_framework.viewsets import ModelViewSet, ReadOnlyModelViewSet

from netbox.api.viewsets import NetBoxReadOnlyModelViewSet
from netbox_vcs import filtersets
from netbox_vcs.models import ChangeDiff, Context
from . import serializers


class VCSRootView(APIRootView):
    def get_view_name(self):
        return 'VCS'


class ContextViewSet(ModelViewSet):
    queryset = Context.objects.all()
    serializer_class = serializers.ContextSerializer
    filterset_class = filtersets.ContextFilterSet


class ChangeDiffViewSet(NetBoxReadOnlyModelViewSet):
    queryset = ChangeDiff.objects.all()
    serializer_class = serializers.ChangeDiffSerializer
    filterset_class = filtersets.ChangeDiffFilterSet
