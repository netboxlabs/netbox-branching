from netbox.search import SearchIndex, register_search
from . import models


@register_search
class BranchIndex(SearchIndex):
    model = models.Branch
    fields = (
        ('name', 100),
        ('description', 500),
        ('comments', 5000),
    )
    display_attrs = ('description', 'description')
