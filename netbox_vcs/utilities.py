from netbox.context import current_request

__all__ = (
    'get_active_context',
)


def get_active_context():
    if request := current_request.get():
        return request.COOKIES.get('active_context')
