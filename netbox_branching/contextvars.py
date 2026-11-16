from contextvars import ContextVar

__all__ = (
    'active_branch',
    'unreplicated_models',
)


active_branch = ContextVar('active_branch', default=None)

# (app_label, model_name) of the models whose table the branch being migrated lacks
unreplicated_models = ContextVar('unreplicated_models', default=frozenset())
