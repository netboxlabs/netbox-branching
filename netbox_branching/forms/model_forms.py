from django.utils.translation import gettext_lazy as _

from netbox.forms import NetBoxModelForm
from netbox_branching.models import Branch
from utilities.forms.fields import CommentField
from utilities.forms.rendering import FieldSet

__all__ = ('BranchForm',)


class BranchForm(NetBoxModelForm):
    fieldsets = (FieldSet('name', 'description', 'tags', name=_('Branch')),)
    comments = CommentField()

    class Meta:
        model = Branch
        fields = ('name', 'description', 'comments', 'tags')
