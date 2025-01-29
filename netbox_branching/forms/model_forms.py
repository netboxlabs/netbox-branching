from django.utils.translation import gettext_lazy as _

from netbox.forms import NetBoxModelForm
from netbox_branching.models import Branch
from utilities.forms.fields import CommentField, DynamicModelChoiceField
from utilities.forms.rendering import FieldSet

__all__ = (
    'BranchForm',
)


class BranchForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'clone_from', 'description', 'tags'),
    )
    clone_from = DynamicModelChoiceField(
        label=_('Clone from'),
        queryset=Branch.objects.all(),
        required=False
    )
    comments = CommentField()

    class Meta:
        model = Branch
        fields = ('name', 'clone_from', 'description', 'comments', 'tags')

    def save(self, *args, **kwargs):

        if clone_from := self.cleaned_data.get('clone_from'):
            self.instance._clone_from = clone_from

        return super().save(*args, **kwargs)
