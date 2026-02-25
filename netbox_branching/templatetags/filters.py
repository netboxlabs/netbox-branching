from django import template

register = template.Library()

EMPTY_VALUES = (None, '', [], {})


@register.filter
def compact_items(value):
    """Return dict items, omitting empty values (None, '', [], {})."""
    if not isinstance(value, dict):
        return []
    return [(k, v) for k, v in value.items() if v not in EMPTY_VALUES]
