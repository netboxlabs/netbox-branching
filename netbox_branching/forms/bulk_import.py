from netbox.forms import NetBoxModelImportForm

from netbox_branching.models import Branch

__all__ = (
    'BranchImportForm',
)


class BranchImportForm(NetBoxModelImportForm):

    class Meta:
        model = Branch
        fields = (
            'name', 'description', 'comments', 'tags',
        )
