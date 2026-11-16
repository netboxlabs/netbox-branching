from collections import defaultdict

import django_tables2 as tables
from django.contrib.contenttypes.models import ContentType
from django.db import DEFAULT_DB_ALIAS
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from core.models import ObjectChange
from netbox.tables import BaseTable, NetBoxTable, columns
from netbox_branching.constants import QUERY_PARAM
from netbox_branching.models import Branch, ChangeDiff
from utilities.templatetags.builtins.filters import placeholder

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
{% if record.changed_object_url %}
    <a href="{{ record.changed_object_url }}">{{ record.object_repr }}</a>
{% else %}
    {{ record.object_repr }}
{% endif %}
"""

CHANGEDIFF_OBJECT = """
{% with url=record.get_object_url %}
{% if url %}
    <a href="{{ url }}">{{ record.object_repr }}</a>
{% else %}
    {{ record.object_repr }}
{% endif %}
{% endwith %}
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

GROUPED_TYPE = '{% if record.changed_object_type %}{{ record.changed_object_type.name|capfirst }}{% endif %}'

GROUPED_REQUEST_ID = '<a href="?request_id={{ record.request_id }}">{{ record.request_id }}</a>'

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


def _rendered_records(table):
    """
    Return the records of the rows about to be rendered: the current page's, if the table is paginated.
    """
    if hasattr(table, 'page'):
        return [row.record for row in table.page.object_list]
    return list(table.data)


class BranchTable(NetBoxTable):
    name = tables.Column(verbose_name=_('Name'), linkify=True)
    is_active = columns.BooleanColumn(verbose_name=_('Active'))
    status = columns.ChoiceFieldColumn(verbose_name=_('Status'))
    is_stale = columns.BooleanColumn(
        true_mark=mark_safe('<span class="text-danger"><i class="mdi mdi-alert-circle"></i></span>'),
        false_mark=None,
        verbose_name=_('Stale'),
    )
    conflicts = ConflictsColumn(verbose_name=_('Conflicts'))
    schema_id = tables.TemplateColumn(template_code='<span class="font-monospace">{{ value }}</span>')
    tags = columns.TagColumn(url_name='plugins:netbox_branching:branch_list')

    class Meta(NetBoxTable.Meta):
        model = Branch
        fields = (
            'pk',
            'id',
            'name',
            'is_active',
            'status',
            'is_stale',
            'conflicts',
            'schema_id',
            'description',
            'owner',
            'tags',
            'created',
            'last_updated',
        )
        default_columns = (
            'pk',
            'name',
            'is_active',
            'status',
            'is_stale',
            'owner',
            'conflicts',
            'schema_id',
            'description',
        )

    def render_is_active(self, value):
        if value:
            return mark_safe('<span class="text-success"><i class="mdi mdi-check-bold"></i></span>')
        return placeholder('')


class ChangeDiffTable(NetBoxTable):
    id = tables.Column(verbose_name=_('ID'), linkify=True)
    branch = tables.Column(verbose_name=_('Branch'), linkify=True)
    # Rendered from ChangeDiff.get_object_url() rather than the object GenericForeignKey, whose prefetch would also
    # look in main for objects created in the branch
    object = tables.TemplateColumn(
        accessor=tables.A('object_repr'), template_code=CHANGEDIFF_OBJECT, verbose_name=_('Object'), orderable=False
    )
    action = columns.ChoiceFieldColumn(
        verbose_name=_('Action'),
    )
    conflicts = ConflictsColumn(verbose_name=_('Conflicts'))
    original_diff = DiffColumn(show_conflicts=False, orderable=False, verbose_name=_('Main (original)'))
    modified_diff = DiffColumn(orderable=False, verbose_name=_('Branch (current)'))
    current_diff = DiffColumn(orderable=False, verbose_name=_('Main (current)'))
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = ChangeDiff
        fields = (
            'id',
            'branch',
            'object_type',
            'object',
            'action',
            'conflicts',
            'original_diff',
            'modified_diff',
            'current_diff',
            'last_updated',
            'actions',
        )
        default_columns = (
            'id',
            'branch',
            'object',
            'action',
            'conflicts',
            'original_diff',
            'modified_diff',
            'current_diff',
        )

    def before_render(self, request):
        super().before_render(request)
        # Look up the changed objects of the rows being rendered together, rather than one row at a time
        ChangeDiff.resolve_objects(_rendered_records(self))


class ChangesTable(NetBoxTable):
    time = columns.DateTimeColumn(verbose_name=_('Time'), timespec='minutes', linkify=True)
    action = columns.ChoiceFieldColumn(
        verbose_name=_('Action'),
    )
    model = tables.Column()
    changed_object_type = columns.ContentTypeColumn(verbose_name=_('Type'))
    # Rendered from the changed object looked up in before_render(), rather than prefetching the changed_object
    # GenericForeignKey
    object_repr = tables.TemplateColumn(
        template_code=OBJECTCHANGE_OBJECT,
        verbose_name=_('Object'),
        orderable=False,
    )
    before = tables.TemplateColumn(
        accessor=tables.A('prechange_data_clean'), template_code=BEFORE_DIFF, verbose_name=_('Before'), orderable=False
    )
    after = tables.TemplateColumn(
        accessor=tables.A('postchange_data_clean'), template_code=AFTER_DIFF, verbose_name=_('After'), orderable=False
    )
    request_id = tables.TemplateColumn(template_code=OBJECTCHANGE_REQUEST_ID, verbose_name=_('Request ID'))
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = ObjectChange
        fields = (
            'pk',
            'time',
            'action',
            'model',
            'changed_object_type',
            'object_repr',
            'request_id',
            'before',
            'after',
        )

    # The branch whose changes are listed, set by the view
    branch = None

    def before_render(self, request):
        super().before_render(request)
        # Look up each changed object in the database its change was read from (the branch, on Changes Ahead), and
        # link an object in the branch with the branch activated. Prefetching the changed_object GenericForeignKey
        # would go through the router, which reads from main unless the branch is active.
        changed_object = ObjectChange._meta.get_field('changed_object')
        groups = defaultdict(list)
        for record in _rendered_records(self):
            if record.changed_object_type_id:
                groups[(record._state.db, record.changed_object_type_id)].append(record)
        for (using, object_type_id), records in groups.items():
            model = ContentType.objects.get_for_id(object_type_id).model_class()
            pks = {record.changed_object_id for record in records}
            objects = model._base_manager.using(using).in_bulk(pks) if model else {}
            query = f'?{QUERY_PARAM}={self.branch.schema_id}' if self.branch and using != DEFAULT_DB_ALIAS else ''
            for record in records:
                obj = objects.get(record.changed_object_id)
                changed_object.set_cached_value(record, obj)
                if obj is not None and hasattr(obj, 'get_absolute_url'):
                    record.changed_object_url = f'{obj.get_absolute_url()}{query}'


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
            'time',
            'user_name',
            'changed_object_type',
            'request_id',
            'creates',
            'updates',
            'deletes',
        )
        default_columns = fields
