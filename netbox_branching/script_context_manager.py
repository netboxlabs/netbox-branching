from contextlib import nullcontext
from .utilities import activate_branch, get_active_branch


def NetBoxScriptContextManager(request):
    branch = get_active_branch(request)
    if branch:
        return activate_branch(branch)
    else:
        return nullcontext()
