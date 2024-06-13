from netbox_vcs.models import Context

from netbox.forms import NetBoxModelForm
from utilities.forms.rendering import FieldSet

__all__ = (
    'ContextForm',
)


class ContextForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'description', 'tags'),
    )

    class Meta:
        model = Context
        fields = ('name', 'description', 'tags')
