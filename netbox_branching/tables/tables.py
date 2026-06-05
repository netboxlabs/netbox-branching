import django_tables2 as tables
from core.models import ObjectChange
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from netbox.tables import BaseTable, NetBoxTable, columns
from utilities.templatetags.builtins.filters import placeholder

from netbox_branching.models import Branch, ChangeDiff

from .columns import ConflictsColumn, DiffColumn

__all__ = (
    'BranchTable',
    'ChangeDiffTable',
    'ChangesGroupedTable',
    'ChangesTable',
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
{% load branch_filters %}
{% if record.action == 'create' %}
    {{ ''|placeholder }}
{% elif record.action == 'delete' %}
    <pre class="p-0">{% for k, v in record.diff.pre.items %}{% if not v|is_empty %}{{ k }}: {{ v }}
{% endif %}{% endfor %}</pre>
{% else %}
    <pre class="p-0">{% for k, v in record.diff.pre.items %}{{ k }}: {{ v }}
{% endfor %}</pre>
{% endif %}
"""

AFTER_DIFF = """
{% load branch_filters %}
{% if record.action == 'delete' %}
    {{ ''|placeholder }}
{% elif record.action == 'create' %}
    <pre class="p-0">{% for k, v in record.diff.post.items %}{% if not v|is_empty %}{{ k }}: {{ v }}
{% endif %}{% endfor %}</pre>
{% else %}
    <pre class="p-0">{% for k, v in record.diff.post.items %}{{ k }}: {{ v }}
{% endfor %}</pre>
{% endif %}
"""

OBJECTCHANGE_REQUEST_ID = """
<a href="?request_id={{ value }}">{{ value }}</a>
"""

GROUPED_TYPE = (
    '{% if record.changed_object_type %}'
    '{{ record.changed_object_type.name|capfirst }}'
    '{% endif %}'
)

GROUPED_REQUEST_ID = (
    '<a href="?request_id={{ record.request_id }}">{{ record.request_id }}</a>'
)

GROUPED_COUNT = (
    '{% load helpers %}'
    '{% if value %}'
    '<a href="?request_id={{ record.request_id }}'
    '&changed_object_type_id={{ record.changed_object_type_id }}'
    '&action={{ action }}">{{ value }}</a>'
    '{% else %}'
    "{{ ''|placeholder }}"
    '{% endif %}'
)


class BranchTable(NetBoxTable):
    name = tables.Column(
        verbose_name=_('Name'),
        linkify=True
    )
    is_active = columns.BooleanColumn(
        verbose_name=_('Active')
    )
    status = columns.ChoiceFieldColumn(
        verbose_name=_('Status')
    )
    is_stale = columns.BooleanColumn(
        true_mark=mark_safe('<span class="text-danger"><i class="mdi mdi-alert-circle"></i></span>'),
        false_mark=None,
        verbose_name=_('Stale')
    )
    conflicts = ConflictsColumn(
        verbose_name=_('Conflicts')
    )
    schema_id = tables.TemplateColumn(
        template_code='<span class="font-monospace">{{ value }}</code>'
    )
    tags = columns.TagColumn(
        url_name='plugins:netbox_branching:branch_list'
    )

    class Meta(NetBoxTable.Meta):
        model = Branch
        fields = (
            'pk', 'id', 'name', 'is_active', 'status', 'is_stale', 'conflicts', 'schema_id', 'description', 'owner',
            'tags', 'created', 'last_updated',
        )
        default_columns = (
            'pk', 'name', 'is_active', 'status', 'is_stale', 'owner', 'conflicts', 'schema_id', 'description',
        )

    def render_is_active(self, value):
        if value:
            return mark_safe('<span class="text-success"><i class="mdi mdi-check-bold"></i></span>')
        return placeholder('')


class ChangeDiffTable(NetBoxTable):
    id = tables.Column(
        verbose_name=_('ID'),
        linkify=True
    )
    branch = tables.Column(
        verbose_name=_('Branch'),
        linkify=True
    )
    object = tables.TemplateColumn(
        template_code=OBJECTCHANGE_OBJECT,
        verbose_name=_('Object'),
        orderable=False
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
        verbose_name=_('Main (original)')
    )
    modified_diff = DiffColumn(
        orderable=False,
        verbose_name=_('Branch (current)')
    )
    current_diff = DiffColumn(
        orderable=False,
        verbose_name=_('Main (current)')
    )
    actions = columns.ActionsColumn(
        actions=()
    )

    class Meta(NetBoxTable.Meta):
        model = ChangeDiff
        fields = (
            'id', 'branch', 'object_type', 'object', 'action', 'conflicts', 'original_diff', 'modified_diff',
            'current_diff', 'last_updated', 'actions',
        )
        default_columns = (
            'id', 'branch', 'object', 'action', 'conflicts', 'original_diff', 'modified_diff', 'current_diff',
        )


class ChangesTable(NetBoxTable):
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
    request_id = tables.TemplateColumn(
        template_code=OBJECTCHANGE_REQUEST_ID,
        verbose_name=_('Request ID')
    )
    actions = columns.ActionsColumn(
        actions=()
    )

    class Meta(NetBoxTable.Meta):
        model = ObjectChange
        fields = (
            'pk', 'time', 'action', 'model', 'changed_object_type', 'object_repr', 'request_id', 'before', 'after',
        )


class ChangesGroupedTable(BaseTable):
    """
    Aggregated view of ObjectChange records: one row per (request_id, changed_object_type).
    Rows are dicts produced by `.values().annotate(...)` in the view.
    """
    time = columns.DateTimeColumn(
        verbose_name=_('Time'),
        timespec='minutes',
        accessor='time',
    )
    user_name = tables.Column(
        verbose_name=_('User'),
        accessor='user_name',
    )
    changed_object_type = tables.TemplateColumn(
        template_code=GROUPED_TYPE,
        verbose_name=_('Type'),
        order_by='changed_object_type_id',
    )
    creates = tables.TemplateColumn(
        template_code=GROUPED_COUNT,
        extra_context={'action': 'create'},
        verbose_name=_('Created'),
        attrs={'td': {'class': 'text-end'}, 'th': {'class': 'text-end'}},
    )
    updates = tables.TemplateColumn(
        template_code=GROUPED_COUNT,
        extra_context={'action': 'update'},
        verbose_name=_('Updated'),
        attrs={'td': {'class': 'text-end'}, 'th': {'class': 'text-end'}},
    )
    deletes = tables.TemplateColumn(
        template_code=GROUPED_COUNT,
        extra_context={'action': 'delete'},
        verbose_name=_('Deleted'),
        attrs={'td': {'class': 'text-end'}, 'th': {'class': 'text-end'}},
    )
    request_id = tables.TemplateColumn(
        template_code=GROUPED_REQUEST_ID,
        verbose_name=_('Request ID'),
        order_by='request_id',
    )

    class Meta(BaseTable.Meta):
        model = ObjectChange
        fields = (
            'time', 'user_name', 'changed_object_type', 'request_id', 'creates', 'updates', 'deletes',
        )
        default_columns = fields
