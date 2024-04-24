from netbox.forms import NetBoxModelForm
from utilities.forms.rendering import FieldSet

from .models import Context


class ContextForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'description'),
    )

    class Meta:
        model = Context
        fields = (
            'name', 'description',
        )
