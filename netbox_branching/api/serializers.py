from core.choices import ObjectChangeActionChoices
from drf_spectacular.utils import extend_schema_field
from netbox.api.exceptions import SerializerNotFound
from netbox.api.fields import ChoiceField, ContentTypeField
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers
from users.api.serializers import UserSerializer
from utilities.api import get_serializer_for_model

from netbox_branching.choices import BranchEventTypeChoices, BranchStatusChoices
from netbox_branching.models import Branch, BranchEvent, ChangeDiff

__all__ = (
    'BranchEventSerializer',
    'BranchSerializer',
    'BranchableModelSerializer',
    'ChangeDiffSerializer',
    'CommitSerializer',
    'ConflictResponseSerializer',
    'ConflictSummarySerializer',
)


class BranchSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_branching-api:branch-detail'
    )
    owner = UserSerializer(
        nested=True,
        read_only=True
    )
    merged_by = UserSerializer(
        nested=True,
        read_only=True
    )
    status = ChoiceField(
        choices=BranchStatusChoices,
        read_only=True
    )

    class Meta:
        model = Branch
        fields = (
            'id', 'url', 'display', 'name', 'status', 'owner', 'description', 'schema_id', 'last_sync', 'merged_time',
            'merged_by', 'comments', 'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'status', 'description')

    def create(self, validated_data):
        """
        Record the user who created the Branch as its owner.
        """
        validated_data['owner'] = self.context['request'].user
        return super().create(validated_data)


class BranchEventSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_branching-api:branchevent-detail'
    )
    branch = BranchSerializer(
        nested=True,
        read_only=True
    )
    user = UserSerializer(
        nested=True,
        read_only=True
    )
    type = ChoiceField(
        choices=BranchEventTypeChoices,
        read_only=True
    )

    class Meta:
        model = BranchEvent
        fields = (
            'id', 'url', 'display', 'time', 'branch', 'user', 'type',
        )
        brief_fields = ('id', 'url', 'display')


class ChangeDiffSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_branching-api:changediff-detail'
    )
    branch = BranchSerializer(
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
    diff = serializers.JSONField(
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
        fields = (
            'id', 'url', 'display', 'branch', 'object_type', 'object_id', 'object', 'object_repr', 'action',
            'conflicts', 'diff', 'original_data', 'modified_data', 'current_data', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'object_type', 'object_id', 'object_repr', 'action')

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


class ConflictSummarySerializer(serializers.ModelSerializer):
    """
    Compact read-only representation of a conflicting ChangeDiff, included inline
    in HTTP 409 responses from the sync and merge actions.
    """
    object_type = ContentTypeField(read_only=True)
    action = ChoiceField(choices=ObjectChangeActionChoices, read_only=True)
    conflicting_data = serializers.SerializerMethodField()

    class Meta:
        model = ChangeDiff
        fields = ('id', 'object_type', 'object_id', 'object_repr', 'action', 'conflicts', 'conflicting_data',
                  'last_updated')

    def get_conflicting_data(self, obj):
        """
        Return the original, branch, and main values for only the conflicting fields.
        """
        if not obj.conflicts:
            return None
        return {
            'original': {k: v for k, v in (obj.original or {}).items() if k in obj.conflicts},
            'branch': {k: v for k, v in (obj.modified or {}).items() if k in obj.conflicts},
            'main': {k: v for k, v in (obj.current or {}).items() if k in obj.conflicts},
        }


class ConflictResponseSerializer(serializers.Serializer):
    """
    Shape of the HTTP 409 response body returned by the sync and merge actions.
    """
    detail = serializers.CharField()
    conflicts = ConflictSummarySerializer(many=True)


class CommitSerializer(serializers.Serializer):
    commit = serializers.BooleanField(required=False)
    acknowledge_conflicts = serializers.BooleanField(required=False, default=False)


class BranchableModelSerializer(serializers.Serializer):
    app_label = serializers.CharField(read_only=True)
    model = serializers.CharField(read_only=True)
    verbose_name = serializers.CharField(read_only=True, allow_null=True)
    verbose_name_plural = serializers.CharField(read_only=True, allow_null=True)
