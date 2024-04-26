# Temporary placeholder for stuff we still need to build
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q


def get_relevant_content_types():
    return ContentType.objects.filter(
        Q(app_label='dcim', model__in=['site', 'sitegroup', 'tenant']) |
        Q(app_label='tenancy', model__in=['tenant', 'tenantgroup'])
    )


def get_tables_to_replicate():
    return (
        'dcim_region',
        'dcim_site',
        'dcim_sitegroup',
        'tenancy_tenant',
        'tenancy_tenantgroup',
    )
