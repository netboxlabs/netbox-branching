from django.utils.translation import gettext_lazy as _
from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label='Branching',
    groups=(
        (_('Branches'), (
            PluginMenuItem(
                link='plugins:netbox_branching:branch_list',
                link_text=_('Branches'),
                auth_required=True,
                permissions=['netbox_branching.view_branch'],
                buttons=(
                    PluginMenuButton(
                        'plugins:netbox_branching:branch_add',
                        _('Add'),
                        'mdi mdi-plus-thick',
                        permissions=['netbox_branching.add_branch'],
                    ),
                    PluginMenuButton(
                        'plugins:netbox_branching:branch_bulk_import',
                        _('Import'),
                        'mdi mdi-upload',
                        permissions=['netbox_branching.add_branch'],
                    ),
                )
            ),
            PluginMenuItem(
                link='plugins:netbox_branching:changediff_list',
                link_text=_('Change Diffs'),
                auth_required=True,
                permissions=['netbox_branching.view_changediff'],
            ),
        )),
    ),
    icon_class='mdi mdi-source-branch'
)
