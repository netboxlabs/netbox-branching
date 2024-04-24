from netbox.api.viewsets import NetBoxReadOnlyModelViewSet
from . import serializers
from ..models import Context


class ContextViewSet(NetBoxReadOnlyModelViewSet):
    queryset = Context.objects.all()
    serializer_class = serializers.ContextSerializer
    # filterset_class = filtersets.ContextFilterSet
