from django import forms
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
        FieldSet('name', 'description', 'clone_from', 'atomic', 'tags'),
    )
    clone_from = DynamicModelChoiceField(
        label=_('Clone from'),
        queryset=Branch.objects.all(),
        required=False
    )
    atomic = forms.BooleanField(
        label=_('Atomic'),
        required=False,
        initial=True,
        help_text=_('Clone only if all changes from the source branch are applied successfully.')
    )
    comments = CommentField()

    class Meta:
        model = Branch
        fields = ('name', 'description', 'clone_from', 'atomic', 'comments', 'tags')

    def save(self, *args, **kwargs):

        if clone_from := self.cleaned_data.get('clone_from'):
            self.instance._clone_source = clone_from
            self.instance._clone_atomic = self.cleaned_data['atomic']

        return super().save(*args, **kwargs)
