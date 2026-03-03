from django import template
from django_filters.constants import EMPTY_VALUES

register = template.Library()


@register.filter
def is_empty(value):
    return value in EMPTY_VALUES
