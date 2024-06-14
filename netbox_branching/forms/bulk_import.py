from netbox_branching.models import Branch

from netbox.forms import NetBoxModelImportForm

__all__ = (
    'BranchImportForm',
)


class BranchImportForm(NetBoxModelImportForm):

    class Meta:
        model = Branch
        fields = (
            'name', 'description', 'comments', 'tags',
        )
