from collections import namedtuple
from contextlib import contextmanager
from unittest import mock

from netbox.registry import registry

from netbox_branching.backends import PLUGIN_NAME
from netbox_branching.models import Branch

__all__ = (
    'DEFAULT_HOST_CONFIG',
    'fetchall',
    'fetchone',
    'plugin_disabled',
    'provision_branch',
)

#: The default host configuration: no `backend`, so get_branching_backend() resolves
#: SchemaBranchingBackend, and no `backend_config`, so every backend's get_config() sees
#: only its own defaults.
#:
#: Test cases covering the shipped backend or the base contract pin this with
#: @override_settings(PLUGINS_CONFIG=DEFAULT_HOST_CONFIG) rather than reading whatever the
#: host happens to configure. Without it they fail on a host set up for an out-of-tree
#: backend -- which is exactly the host a backend author runs this suite on, and what
#: docs/plugin-development.md invites them to do.
DEFAULT_HOST_CONFIG = {PLUGIN_NAME: {}}


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
