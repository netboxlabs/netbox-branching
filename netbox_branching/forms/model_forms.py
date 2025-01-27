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
        FieldSet('name', 'origin', 'description', 'tags'),
    )
    origin = DynamicModelChoiceField(
        label=_('Origin'),
        queryset=Branch.objects.all(),
        required=False
    )
    comments = CommentField()

    class Meta:
        model = Branch
        fields = ('name', 'origin', 'description', 'comments', 'tags')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance.pk:
            # Originating branch is cannot be modified
            self.fields['origin'].disabled = True
