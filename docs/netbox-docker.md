# NetBox Branching with NetBox Docker

NetBox Docker is _not_ officially supported by NetBox Labs. This page is intended as a _minimal_ guide to getting branching working on NetBox Docker. For additional configuration options, please consult the [NetBox Docker documentation.](https://github.com/netbox-community/netbox-docker)

## Clone NetBox Docker and step into the repo

```
git clone --branch 3.4.1 https://github.com/netbox-community/netbox-docker.git
pushd netbox-docker
```

## Create a Dockerfile to install NetBox Branching

```
cat <<EOF > Dockerfile-Plugins
FROM netboxcommunity/netbox:v4.4.6
RUN uv pip install netboxlabs-netbox-branching==0.7.2
EOF
```
 
## Create a docker-compose.override.yaml to include the custom image

```
cat <<EOF > docker-compose.override.yml
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
EOF
```

## Create plugins.py to configure branching

> [!TIP]
> Remember to insert your postgres password, the default for which can be found in `env/postgres.env`  

```
cat <<EOF > configuration/plugins.py
PLUGINS = ["netbox_branching"] # If you have multiple plugins, netbox-branching _must_ come last

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
EOF
```

## Build the NetBox image

```
docker compose build --no-cache
```

## Start NetBox Docker

```
docker compose up -d
```