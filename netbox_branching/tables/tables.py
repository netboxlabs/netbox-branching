import django_tables2 as tables
from django.utils.translation import gettext_lazy as _

from core.models import ObjectChange
from netbox.tables import NetBoxTable, columns
from netbox_branching.models import Branch, ChangeDiff
from .columns import ConflictsColumn, DiffColumn

__all__ = (
    'ChangeDiffTable',
    'BranchTable',
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


class BranchTable(NetBoxTable):
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
    conflicts = ConflictsColumn(
        verbose_name=_('Conflicts')
    )
    schema_id = tables.TemplateColumn(
        template_code='<span class="font-monospace">{{ value }}</code>'
    )

    class Meta(NetBoxTable.Meta):
        model = Branch
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
    branch = tables.Column(
        verbose_name=_('Branch'),
        linkify=True
    )
    object = tables.Column(
        verbose_name=_('Object'),
        linkify=True
    )
    action = columns.ChoiceFieldColumn(
        verbose_name=_('Action'),
    )
    conflicts = ConflictsColumn(
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
            'branch', 'object_type', 'object', 'action', 'conflicts', 'original_diff', 'modified_diff', 'current_diff',
            'last_updated', 'actions',
        )
        default_columns = ('branch', 'object', 'action', 'conflicts', 'original_diff', 'modified_diff', 'current_diff')


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