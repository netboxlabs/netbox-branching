import django_tables2 as tables
from django.utils.translation import gettext_lazy as _

from core.tables import ObjectChangeTable
from utilities.tables import register_table_column

__all__ = (
    'ConflictsColumn',
    'DiffColumn',
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


class ConflictsColumn(tables.TemplateColumn):
    template_code = """
    {% if record.conflicts %}
      <span class="text-red"><i class="mdi mdi-alert-octagon"></i></span>
    {% else %}
      {{ ''|placeholder }}
    {% endif %}
    """

    def __init__(self, *args, **kwargs):
        super().__init__(template_code=self.template_code, *args, **kwargs)


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


# Register a table column to include the Branch associated with each record in ObjectChangeTable
branch_column = tables.Column(
    accessor=tables.A('application__branch'),
    linkify=True,
    verbose_name=_('Branch')
)
register_table_column(branch_column, 'branch', ObjectChangeTable)
