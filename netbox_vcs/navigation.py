from django.utils.translation import gettext_lazy as _

from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label='VCS',
    groups=(
        (_('Contexts'), (
            PluginMenuItem(
                link='plugins:netbox_vcs:context_list',
                link_text='Contexts',
                buttons=(
                    PluginMenuButton('plugins:netbox_vcs:context_add', _('Add'), 'mdi mdi-plus-thick'),
                    PluginMenuButton('plugins:netbox_vcs:context_import', _('Import'), 'mdi mdi-upload'),
                )
            ),
            PluginMenuItem(
                link='plugins:netbox_vcs:changediff_list',
                link_text='Changes'
            ),
        )),
    ),
    icon_class='mdi mdi-source-branch'
)
