from .utilities import activate_branch, get_active_branch

class BranchingBackend:
    def activate_branch(self, branch):
        return activate_branch(branch)

    def get_active_branch(self, request):
        return get_active_branch(request)
