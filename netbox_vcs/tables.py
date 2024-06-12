import django_tables2 as tables
from django.utils.translation import gettext_lazy as _

from extras.models import ObjectChange
from netbox.tables import NetBoxTable, columns
from .models import Context, ChangeDiff

__all__ = (
    'ChangeDiffTable',
    'ContextTable',
    'DiffColumn',
    'ReplayTable',
)


OBJECTCHANGE_FULL_NAME = """
{% load helpers %}
{{ value.get_full_name|placeholder }}
"""

OBJECTCHANGE_OBJECT = """
{% if value and value.get_absolute_url %}
    <a href="{{ value.get_absolute_url }}">{{ record.object_repr }}</a>
{% else %}
    {{ record.object_repr }}
{% endif %}
"""

BEFORE_DIFF = """
{% if record.action == 'create' %}
    {{ ''|placeholder }}
{% else %}
    <pre class="p-0">{% for k, v in record.diff.pre.items %}{{ k }}: {{ v }}
{% endfor %}</pre>
{% endif %}
"""

AFTER_DIFF = """
{% if record.action == 'delete' %}
    {{ ''|placeholder }}
{% else %}
    <pre class="p-0">{% for k, v in record.diff.post.items %}{{ k }}: {{ v }}
{% endfor %}</pre>
{% endif %}
"""


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
    context = tables.Column(
        verbose_name=_('Context'),
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
            'context', 'object_type', 'object', 'action', 'conflicts', 'original_diff', 'modified_diff', 'current_diff',
            'last_updated', 'actions',
        )
        default_columns = ('context', 'object', 'action', 'conflicts', 'original_diff', 'modified_diff', 'current_diff')


class ReplayTable(NetBoxTable):
    time = columns.DateTimeColumn(
        verbose_name=_('Time'),
        timespec='minutes',
        linkify=True
    )
    action = columns.ChoiceFieldColumn(
        verbose_name=_('Action'),
    )
    model = tables.Column()
    changed_object_type = columns.ContentTypeColumn(
        verbose_name=_('Type')
    )
    object_repr = tables.TemplateColumn(
        accessor=tables.A('changed_object'),
        template_code=OBJECTCHANGE_OBJECT,
        verbose_name=_('Object'),
        orderable=False
    )
    before = tables.TemplateColumn(
        accessor=tables.A('prechange_data_clean'),
        template_code=BEFORE_DIFF,
        verbose_name=_('Before'),
        orderable=False
    )
    after = tables.TemplateColumn(
        accessor=tables.A('postchange_data_clean'),
        template_code=AFTER_DIFF,
        verbose_name=_('After'),
        orderable=False
    )
    actions = columns.ActionsColumn(
        actions=()
    )

    class Meta(NetBoxTable.Meta):
        model = ObjectChange
        fields = (
            'pk', 'time', 'action', 'model', 'changed_object_type', 'object_repr', 'before', 'after',
        )
