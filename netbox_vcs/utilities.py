from netbox.context import current_request

from .constants import SCHEMA_PREFIX

__all__ = (
    'DynamicSchemaDict',
    'get_active_context',
)


class DynamicSchemaDict(dict):
    """
    Behaves like a normal dictionary, except for keys beginning with "schema_". Any lookup for
    "schema_*" will return the default configuration extended to include the search_path option.
    """
    def __getitem__(self, item):
        if type(item) is str and item.startswith('schema_'):
            if schema := item.removeprefix('schema_'):
                default_config = super().__getitem__('default')
                schema_name = f'{SCHEMA_PREFIX}{schema}'
                return {
                    **default_config,
                    'OPTIONS': {
                        'options': f'-c search_path={schema_name},public'
                    }
                }
        return super().__getitem__(item)

    def __contains__(self, item):
        if type(item) is str and item.startswith('schema_'):
            return True
        return super().__contains__(item)


def get_active_context():
    if request := current_request.get():
        return request.COOKIES.get('active_context')
