from django.dispatch import Signal

__all__ = (
    'post_deprovision',
    'post_merge',
    'post_migrate',
    'post_provision',
    'post_revert',
    'post_sync',
    'pre_deprovision',
    'pre_merge',
    'pre_migrate',
    'pre_provision',
    'pre_revert',
    'pre_sync',
)

# Pre-event signals
pre_provision = Signal()
pre_deprovision = Signal()
pre_sync = Signal()
pre_migrate = Signal()
pre_merge = Signal()
pre_revert = Signal()

# Post-event signals
post_provision = Signal()
post_deprovision = Signal()
post_sync = Signal()
post_migrate = Signal()
post_merge = Signal()
post_revert = Signal()
