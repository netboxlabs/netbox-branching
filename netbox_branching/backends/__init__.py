"""
Pluggable branching backends.

A branching backend encapsulates the mechanism by which a branch's dataset is
isolated from main: how it is created and destroyed, how connections addressing
it are named and configured, and how migrations are applied to it. See
``BranchingBackend`` for the contract and the invariants an implementation must
satisfy.
"""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.utils.module_loading import import_string
from netbox.plugins import get_plugin_config

from .base import BranchingBackend
from .schema import SchemaBranchingBackend

__all__ = (
    'DEFAULT_BACKEND',
    'BranchingBackend',
    'SchemaBranchingBackend',
    'get_branching_backend',
)


PLUGIN_NAME = 'netbox_branching'
DEFAULT_BACKEND = 'netbox_branching.backends.SchemaBranchingBackend'

# Resolved backend instances, keyed by import path. Backends are stateless
# singletons; instantiating one per configured path avoids re-running
# import_string on every routing decision.
_backends = {}


def _plugin_is_enabled():
    """
    Return True if netbox_branching is registered in the host configuration.
    """
    return settings.configured and PLUGIN_NAME in getattr(settings, 'PLUGINS_CONFIG', {})


def get_branching_backend(required=True):
    """
    Return the configured branching backend instance.

    The instance is cached per import path, so a backend may safely memoize
    derived state that does not depend on plugin configuration.

    Args:
        required: If False, return None instead of raising when the plugin is not
                  enabled in the host configuration.
    """
    if not required and not _plugin_is_enabled():
        return None

    # An explicit default is passed rather than relying on default_settings so that
    # tests which replace PLUGINS_CONFIG wholesale via override_settings still resolve
    # a backend.
    path = get_plugin_config(PLUGIN_NAME, 'backend', DEFAULT_BACKEND) or DEFAULT_BACKEND

    if path not in _backends:
        try:
            backend_class = import_string(path)
        except ImportError as e:
            raise ImproperlyConfigured(f"netbox_branching: backend not found: {path}") from e
        if not (isinstance(backend_class, type) and issubclass(backend_class, BranchingBackend)):
            raise ImproperlyConfigured(
                f"netbox_branching: backend {path} is not a subclass of netbox_branching.backends.BranchingBackend."
            )
        _backends[path] = backend_class()

    return _backends[path]


def _clear_backend_cache(**kwargs):
    """
    Discard cached backend instances when settings change, so a backend which has
    memoized configuration does not survive an override_settings() block.
    """
    _backends.clear()


setting_changed.connect(_clear_backend_cache)
