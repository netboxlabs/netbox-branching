# NetBox Branching

This [NetBox](http://netboxlabs.com/oss/netbox/) plugin introduces branching functionality. A branch is a discrete, static snapshot of the NetBox database which can be modified independently and later merged back into the main database. This enables users to make "offline" changes to objects within NetBox and avoid interfering with its integrity as the network source of truth. It also provides the opportunity to review changes in bulk prior to their application.

## Requirements

* NetBox v4.1 or later
* PostgreSQL 12 or later

## Installation

Brief installation instructions are provided below. For a complete installation guide, please refer to the included documentation.

1. Activate the NetBox virtual environment:

```
$ source /opt/netbox/venv/bin/activate
```

2. Install the plugin from [PyPI](https://pypi.org/project/netboxlabs-netbox-branching/):

```
$ pip install netboxlabs-netbox-branching
```

3. Add `netbox_branching` to `PLUGINS` in `configuration.py`:

```python
PLUGINS = [
    # ...
    'netbox_branching',
]
```

4. Create `local_settings.py` to override the `DATABASES` & `DATABASE_ROUTERS` settings. This enables dynamic schema support.

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

5. Run NetBox migrations:

```
$ ./manage.py migrate
```
