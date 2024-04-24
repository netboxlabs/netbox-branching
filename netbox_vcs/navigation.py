from django.utils.translation import gettext_lazy as _

from netbox.plugins import PluginMenu, PluginMenuItem

menu = PluginMenu(
    label='VCS',
    groups=(
        (_('Contexts'), (
            PluginMenuItem(
                link='plugins:netbox_vcs:context_list',
                link_text='Contexts',
            ),
        )),
    ),
    icon_class='mdi mdi-router'
)
