import strawberry
import strawberry_django
from strawberry.types import Info

from .models import Branch

__all__ = (
    'BranchType',
    'ObjectChangeTypeExtension',
    'type_extensions',
)


@strawberry_django.type(
    Branch,
    fields=['id', 'name', 'description', 'status', 'schema_id', 'merged_time'],
)
class BranchType:
    pass


@strawberry.type
class ObjectChangeTypeExtension:
    models = ['core.objectchange']

    @strawberry_django.field(select_related=['application__branch'])
    def branch(self, info: Info) -> BranchType | None:
        """The branch this change was merged from, if any."""
        if not (application := getattr(self, 'application', None)):
            return None
        # Resolve viewable branches once per request rather than one permission query per row
        request = info.context.request
        if (viewable := getattr(request, '_branching_viewable_branch_ids', None)) is None:
            viewable = set(Branch.objects.restrict(request.user, 'view').values_list('pk', flat=True))
            request._branching_viewable_branch_ids = viewable
        return application.branch if application.branch_id in viewable else None


type_extensions = [
    ObjectChangeTypeExtension,
]
