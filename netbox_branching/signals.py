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
    'squash_dependency_graph_built',
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

# Fired by ``SquashMergeStrategy`` after its FK / GFK dependency graph has been
# built and before topological ordering. Receivers may mutate each
# ``CollapsedChange``'s ``depends_on`` / ``depended_by`` sets to add extra
# edges for relationships squash can't see (plugin-defined shapes, models
# that store object references outside Django's standard field types, etc.).
#
# kwargs:
#   collapsed_changes — dict of CollapsedChange keyed by (app.model, pk).
#                       Bidirectional-cycle splits also add synthetic UPDATE
#                       entries keyed by (app.model, pk, 'update_<field>');
#                       match object identity on key[:2]. Mutate in place;
#                       the return value is ignored.
#   operation — 'merge' or 'revert'. Revert reverses the topological order
#               after sorting, so edges added unconditionally still apply in
#               reverse; receivers that only want their edges to influence
#               merge ordering should branch on this kwarg.
squash_dependency_graph_built = Signal()
