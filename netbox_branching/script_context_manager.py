from contextlib import nullcontext
from .utilities import activate_branch, get_active_branch


def NetBoxScriptContextManager(request):
    if branch := get_active_branch(request)
        return activate_branch(branch)
    else:
        return nullcontext()
