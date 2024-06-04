import django_tables2 as tables
from django.utils.translation import gettext_lazy as _

from netbox.tables import NetBoxTable, columns
from .models import Context

__all__ = (
    'ContextTable',
)


class ContextTable(NetBoxTable):
    name = tables.Column(
        verbose_name=_('Name'),
        linkify=True
    )
    is_active = columns.BooleanColumn(
        verbose_name=_('Active')
    )
    status = columns.ChoiceFieldColumn(
        verbose_name=_('Status'),
    )
    schema_id = tables.TemplateColumn(
        template_code='<span class="font-monospace">{{ value }}</code>'
    )

    class Meta(NetBoxTable.Meta):
        model = Context
        fields = (
            'pk', 'id', 'name', 'is_active', 'status', 'schema_id', 'description', 'user', 'tags', 'created',
            'last_updated',
        )
        default_columns = ('pk', 'name', 'is_active', 'status', 'schema_id', 'description', 'user')
