from collections import namedtuple
from contextlib import contextmanager
from unittest import mock

from netbox.registry import registry

from netbox_branching.models import Branch

__all__ = (
    'fetchall',
    'fetchone',
    'plugin_disabled',
    'provision_branch',
)


@contextmanager
def plugin_disabled():
    """
    Simulate the plugin having been dropped from the host's PLUGINS setting.

    What actually changes when an operator comments netbox_branching out of PLUGINS is
    that NetBox never registers it as installed. Their PLUGINS_CONFIG block survives —
    NetBox only ever adds entries to that dict, never removes them — so a test which
    empties PLUGINS_CONFIG instead is pinning a state that cannot occur, and would pass
    against a guard that never fires in production.
    """
    installed = [name for name in registry['plugins']['installed'] if name != 'netbox_branching']
    with mock.patch.dict(registry['plugins'], {'installed': installed}):
        yield


def provision_branch(*, user, name='Test Branch', **kwargs):
    """
    Create and provision a Branch, returning it with status READY.

    Branch.provision() runs synchronously in the calling thread; it writes the
    row via Branch.objects.filter(pk=...).update(...) and mirrors those fields
    onto the in-memory instance. The refresh_from_db() below is belt and braces
    for anything a future provisioning step writes without mirroring.

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
