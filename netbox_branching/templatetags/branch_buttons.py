from django import template

__all__ = (
    'branch_archive_button',
    'branch_merge_button',
    'branch_migrate_button',
    'branch_revert_button',
    'branch_sync_button',
)

EMPTY_VALUES = (None, '', [], {})

register = template.Library()


@register.filter
def compact_items(value):
    """Return dict items, omitting empty values (None, '', [], {})."""
    if not isinstance(value, dict):
        return []
    return [(k, v) for k, v in value.items() if v not in EMPTY_VALUES]


@register.inclusion_tag('netbox_branching/buttons/branch_sync.html', takes_context=True)
def branch_sync_button(context, branch):
    return {
        'branch': branch,
        'perms': context.get('perms'),
    }


@register.inclusion_tag('netbox_branching/buttons/branch_merge.html', takes_context=True)
def branch_merge_button(context, branch):
    return {
        'branch': branch,
        'perms': context.get('perms'),
    }


@register.inclusion_tag('netbox_branching/buttons/branch_revert.html', takes_context=True)
def branch_revert_button(context, branch):
    return {
        'branch': branch,
        'perms': context.get('perms'),
    }


@register.inclusion_tag('netbox_branching/buttons/branch_archive.html', takes_context=True)
def branch_archive_button(context, branch):
    return {
        'branch': branch,
        'perms': context.get('perms'),
    }


@register.inclusion_tag('netbox_branching/buttons/branch_migrate.html', takes_context=True)
def branch_migrate_button(context, branch):
    return {
        'branch': branch,
        'perms': context.get('perms'),
    }
