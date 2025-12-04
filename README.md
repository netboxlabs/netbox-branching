# NetBox Branching

This [NetBox](http://netboxlabs.com/oss/netbox/) plugin introduces branching functionality. A branch is a discrete, static snapshot of the NetBox database which can be modified independently and later merged back into the main database. This enables users to make "offline" changes to objects within NetBox and avoid interfering with its integrity as the network source of truth. It also provides the opportunity to review changes in bulk prior to their application.

## Requirements

* NetBox v4.3 or later
* PostgreSQL 12 or later

> [!TIP]
> To use NetBox Branching with NetBox Docker, click [here](./docs/netbox-docker.md)  

## Installation

Brief installation instructions are provided below. For a complete installation guide, please refer to the included documentation.

1. Grant PostgreSQL permission for the NetBox database user to create schemas:

```postgresql
GRANT CREATE ON DATABASE $database TO $user;
```

2. Activate the NetBox virtual environment:

```
$ source /opt/netbox/venv/bin/activate
```

3. Install the plugin from [PyPI](https://pypi.org/project/netboxlabs-netbox-branching/):

```
$ pip install netboxlabs-netbox-branching
```

4. Add `netbox_branching` to the end of `PLUGINS` in `configuration.py`. Note that `netbox_branching` **MUST** be the last plugin listed.

```python
PLUGINS = [
    # ...
    'netbox_branching',
]
```

5. Configure the database and router in `configuration.py`:

```python
from netbox_branching.utilities import DynamicSchemaDict

DATABASES = DynamicSchemaDict({
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'netbox',               # Database name
        'USER': 'netbox',               # PostgreSQL username
        'PASSWORD': 'password',         # PostgreSQL password
        'HOST': 'localhost',            # Database server
        'PORT': '',                     # Database port (leave blank for default)
        'CONN_MAX_AGE': 300,            # Max database connection age
    }
})

DATABASE_ROUTERS = [
    'netbox_branching.database.BranchAwareRouter',
]
```

6. Run NetBox migrations:

```
$ ./manage.py migrate
```
