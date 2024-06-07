from netbox.plugins import PluginConfig


class NetBoxVCSConfig(PluginConfig):
    name = 'netbox_vcs'
    verbose_name = ' NetBox VCS'
    description = 'A version control system implementation for NetBox'
    version = '0.1'
    base_url = 'vcs'
    # min_version = '4.0'
    middleware = [
        'netbox_vcs.middleware.ContextMiddleware'
    ]

    def ready(self):
        super().ready()
        from . import signals


config = NetBoxVCSConfig
