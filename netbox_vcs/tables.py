import django_tables2 as tables
from django.utils.translation import gettext_lazy as _

from netbox.tables import NetBoxTable, columns
from .models import Context, ChangeDiff

__all__ = (
    'ChangeDiffTable',
    'ContextTable',
    'DiffColumn',
)


class DiffColumn(tables.TemplateColumn):
    template_code = """{% load helpers %}
        {% for k, v in value.items %}
        {{ k }}:
          {% if show_conflicts and k in record.conflicts %}
            <span class="bg-red text-red-fg px-1 rounded-2">{{ v|placeholder }}</span>
          {% elif v != record.original|get_item:k %}
            <span class="bg-green text-green-fg px-1 rounded-2">{{ v|placeholder }}</span>
          {% else %}
            {{ v|placeholder }}
          {% endif %}
        <br />{% endfor %}
        """

    def __init__(self, show_conflicts=True, *args, **kwargs):
        context = {
            'show_conflicts': show_conflicts,
        }
        super().__init__(template_code=self.template_code, extra_context=context, *args, **kwargs)

    def value(self, value):
        return str(value) if value else None


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
    # TODO: Invert checkmark condition
    conflicts = columns.BooleanColumn(
        verbose_name=_('Conflicts')
    )
    schema_id = tables.TemplateColumn(
        template_code='<span class="font-monospace">{{ value }}</code>'
    )

    class Meta(NetBoxTable.Meta):
        model = Context
        fields = (
            'pk', 'id', 'name', 'is_active', 'status', 'conflicts', 'schema_id', 'description', 'user', 'tags',
            'created', 'last_updated',
        )
        default_columns = ('pk', 'name', 'is_active', 'status', 'conflicts', 'schema_id', 'description', 'user')


class ChangeDiffTable(NetBoxTable):
    name = tables.Column(
        verbose_name=_('Name'),
        linkify=True
    )
    object = tables.Column(
        verbose_name=_('Object'),
        linkify=True
    )
    action = columns.ChoiceFieldColumn(
        verbose_name=_('Action'),
    )
    # TODO: Invert checkmark condition
    conflicts = columns.BooleanColumn(
        verbose_name=_('Conflicts')
    )
    original_diff = DiffColumn(
        show_conflicts=False,
        orderable=False,
        verbose_name=_('Original')
    )
    modified_diff = DiffColumn(
        orderable=False,
        verbose_name=_('Modified')
    )
    current_diff = DiffColumn(
        orderable=False,
        verbose_name=_('Current')
    )
    actions = columns.ActionsColumn(
        actions=()
    )

    class Meta(NetBoxTable.Meta):
        model = ChangeDiff
        fields = (
            'context', 'object', 'action', 'conflicts', 'original_diff', 'modified_diff', 'current_diff',
            'last_updated', 'actions',
        )
        default_columns = ('object', 'action', 'conflicts', 'original_diff', 'modified_diff', 'current_diff')
