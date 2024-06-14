from netbox_vcs.models import Branch

from netbox.forms import NetBoxModelForm
from utilities.forms.rendering import FieldSet

__all__ = (
    'BranchForm',
)


class BranchForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'description', 'tags'),
    )

    class Meta:
        model = Branch
        fields = ('name', 'description', 'tags')
