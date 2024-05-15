# nbl-netbox-vcs

### Internal Use Only

Initial proof of concept for multi-context/versioning support in NetBox.

### Initial Setup

1. Activate the NetBox virtual environment

```
$ source /opt/netbox/venv/bin/activate
```

2. Install the plugin from source

```
$ pip install -e .
```

3. Set `DATABASE_ROUTERS` in `configuration.py`

```python
DATABASE_ROUTERS = [
    'netbox_vcs.database.ContextAwareRouter',
]
```

4. Create `local_settings.py` to override the `DATABASES` setting. This enables dynamic schema support.

```python
from netbox_vcs.utilities import DynamicSchemaDict
from .configuration import DATABASE

# Wrap DATABASES with DynamicSchemaDict for dynamic schema support
DATABASES = DynamicSchemaDict({
    'default': DATABASE,
})
```
