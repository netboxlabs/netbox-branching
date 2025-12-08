# Using Branching with NetBox Docker

NetBox Docker is _not_ officially supported by NetBox Labs. This page is intended as a _minimal_ guide to getting branching working on NetBox Docker. For additional configuration options, please consult the [NetBox Docker documentation.](https://github.com/netbox-community/netbox-docker)

## Building a Docker Image with Branching

### 1. Clone the `netbox-docker` repository

```
git clone --branch 3.4.1 https://github.com/netbox-community/netbox-docker.git
pushd netbox-docker
```

### 2. Create a Dockerfile

Create a Dockerfile in the root of the repository to include the `netbox-branching` plugin:

```text title="Dockerfile-Plugins"
FROM netboxcommunity/netbox:v4.4.6
RUN uv pip install netboxlabs-netbox-branching==0.7.2
```
 
### 3. Include the custom image

Create a `docker-compose.override.yml` file to include the custom image:

```yaml title="docker-compose.override.yml"
services:
  netbox:
    image: netbox:v4.4.6-plugins
    pull_policy: never
    ports:
      - "8000:8080"
    build:
      context: .
      dockerfile: Dockerfile-Plugins
    environment:
      SKIP_SUPERUSER: "false"
      SUPERUSER_EMAIL: ""
      SUPERUSER_NAME: "admin"
      SUPERUSER_PASSWORD: "admin"
    healthcheck:
      test: curl -f http://127.0.0.1:8080/login/ || exit 1
      start_period: 600s
      timeout: 3s
      interval: 15s
  postgres:
    ports:
      - "5432:5432"
  netbox-worker:
    image: netbox:v4.4.6-plugins
    pull_policy: never
```

### 4. Configure the plugin

Create `plugins.py` to store the plugin's configuration.

!!! tip
    Remember to insert your postgres password, the default for which can be found in `env/postgres.env`  

```python title="configuration/plugins.py"
# If you have multiple plugins, netbox-branching _must_ come last
PLUGINS = ["netbox_branching"]

from netbox_branching.utilities import DynamicSchemaDict

DATABASES = DynamicSchemaDict({
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'netbox',                 # Database name
        'USER': 'netbox',                 # PostgreSQL username
        'PASSWORD': 'yourpassword',       # PostgreSQL password
        'HOST': 'postgres',               # Database server
        'PORT': '',                       # Database port (leave blank for default)
        'CONN_MAX_AGE': 300,              # Max database connection age
    }
})

DATABASE_ROUTERS = [
    'netbox_branching.database.BranchAwareRouter',
]
```

### 5. Build the NetBox image

```
docker compose build --no-cache
```

### 6. Start NetBox Docker

```
docker compose up -d
```
