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
        application = getattr(self, 'application', None)
        if application and info.context.request.user.has_perm('netbox_branching.view_branch', application.branch):
            return application.branch
        return None


type_extensions = [
    ObjectChangeTypeExtension,
]
