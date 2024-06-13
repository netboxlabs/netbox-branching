from netbox_vcs.models import Context

from netbox.forms import NetBoxModelImportForm

__all__ = (
    'ContextImportForm',
)


class ContextImportForm(NetBoxModelImportForm):

    class Meta:
        model = Context
        fields = (
            'name', 'description', 'tags',
        )