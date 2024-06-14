# nbl-netbox-branching

### Internal Use Only

Initial proof of concept for multi-branch/versioning support in NetBox.

### Initial Setup

1. Activate the NetBox virtual environment

```
$ source /opt/netbox/venv/bin/activate
```

2. Install the plugin from source

```
$ pip install -e .
```

3. Add `netbox_branching` to `PLUGINS` in `configuration.py`

```python
PLUGINS = [
    # ...
    'netbox_branching',
]
```

4. Create `local_settings.py` to override the `DATABASES` setting. This enables dynamic schema support.

```python
from netbox_branching.utilities import DynamicSchemaDict
from .configuration import DATABASE

# Wrap DATABASES with DynamicSchemaDict for dynamic schema support
DATABASES = DynamicSchemaDict({
    'default': DATABASE,
})

# Employ our custom database router
DATABASE_ROUTERS = [
    'netbox_branching.database.BranchAwareRouter',
]
```

5. Run NetBox migrations

```
$ ./manage.py migrate
```
