from rest_framework import serializers

from netbox.api.serializers import NetBoxModelSerializer
from ..models import Context

__all__ = (
    'ContextSerializer',
)


class ContextSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_vcs-api:context-detail'
    )

    class Meta:
        model = Context
        fields = [
            'id', 'url', 'display', 'name', 'description', 'schema_name', 'custom_fields', 'created', 'last_updated',
        ]
        brief_fields = ('id', 'url', 'display', 'name', 'description')
