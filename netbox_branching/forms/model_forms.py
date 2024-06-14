from netbox_branching.models import Branch

from netbox.forms import NetBoxModelForm
from utilities.forms.fields import CommentField
from utilities.forms.rendering import FieldSet

__all__ = (
    'BranchForm',
)


class BranchForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'description', 'tags'),
    )
    comments = CommentField()

    class Meta:
        model = Branch
        fields = ('name', 'description', 'comments', 'tags')
