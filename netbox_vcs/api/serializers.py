from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from extras.choices import ObjectChangeActionChoices
from netbox.api.exceptions import SerializerNotFound
from netbox.api.fields import ChoiceField, ContentTypeField
from netbox.api.serializers import NetBoxModelSerializer
from utilities.api import get_serializer_for_model
from netbox_vcs.models import ChangeDiff, Context

__all__ = (
    'ChangeDiffSerializer',
)


class ContextSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_vcs-api:context-detail'
    )

    class Meta:
        model = Context
        fields = [
            'id', 'url', 'display', 'name', 'description', 'schema_id', 'custom_fields', 'created', 'last_updated',
        ]
        brief_fields = ('id', 'url', 'display', 'name', 'description')


class ChangeDiffSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_vcs-api:changediff-detail'
    )
    context = ContextSerializer(
        nested=True,
        read_only=True
    )
    object_type = ContentTypeField(
        read_only=True
    )
    object = serializers.SerializerMethodField(
        read_only=True
    )
    action = ChoiceField(
        choices=ObjectChangeActionChoices,
        read_only=True
    )
    original_data = serializers.JSONField(
        source='original',
        read_only=True,
        allow_null=True
    )
    modified_data = serializers.JSONField(
        source='modified',
        read_only=True,
        allow_null=True
    )
    current_data = serializers.JSONField(
        source='current',
        read_only=True,
        allow_null=True
    )

    class Meta:
        model = ChangeDiff
        fields = [
            'id', 'url', 'display', 'context', 'object_type', 'object_id', 'object', 'action', 'original_data',
            'modified_data', 'current_data', 'last_updated',
        ]
        brief_fields = ('id', 'url', 'display', 'object_type', 'object_id', 'action')

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_object(self, obj):
        """
        Serialize a nested representation of the changed object.
        """
        if obj.object is None:
            return None

        try:
            serializer = get_serializer_for_model(obj.object)
        except SerializerNotFound:
            return obj.object_repr
        data = serializer(obj.object, nested=True, context={'request': self.context['request']}).data

        return data
