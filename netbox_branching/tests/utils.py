from collections import namedtuple

from netbox_branching.models import Branch

__all__ = (
    'fetchall',
    'fetchone',
    'provision_branch',
)


def provision_branch(*, user, name='Test Branch', **kwargs):
    """
    Create and provision a Branch, returning it with status READY.

    Branch.provision() runs synchronously in the calling thread; it updates
    the row via Branch.objects.filter(pk=...).update(...), which bypasses
    the in-memory instance, so the caller needs refresh_from_db() to see
    the post-provision status.

    Any extra kwargs (e.g. merge_strategy) are passed through to the Branch
    constructor.
    """
    branch = Branch(name=name, **kwargs)
    branch.save(provision=False)
    branch.provision(user=user)
    branch.refresh_from_db()
    return branch


def fetchall(cursor):
    """
    Map cursor.fetchall() into a list of named tuples for convenience.
    """
    result = namedtuple('Result', [col[0] for col in cursor.description])
    return [
        result(*row) for row in cursor.fetchall()
    ]


def fetchone(cursor):
    """
    Map cursor.fetchone() into a named tuple for convenience.
    """
    if ret := cursor.fetchone():
        result = namedtuple('Result', [col[0] for col in cursor.description])
        return result(*ret)
    return None
