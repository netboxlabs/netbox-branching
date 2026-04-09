from django.utils.translation import gettext_lazy as _
from netbox.object_actions import ObjectAction

__all__ = (
    'BulkMigrate',
)


class BulkMigrate(ObjectAction):
    """
    Apply pending database migrations to multiple branches at once.
    """
    name = 'bulk_migrate'
    label = _('Migrate Selected')
    multi = True
    permissions_required = {'migrate'}  # noqa: RUF012
    template_name = 'netbox_branching/buttons/bulk_migrate.html'
