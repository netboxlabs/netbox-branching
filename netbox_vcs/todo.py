# Temporary placeholder for stuff we still need to build
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

MODELS_TO_REPLICATE = (
    'dcim.region',
    'dcim.site',
    'dcim.sitegroup',
    'extras.tag',
    'ipam.asn',
    'tenancy.tenant',
    'tenancy.tenantgroup',
)


def get_relevant_content_types():
    return ContentType.objects.filter(
        Q(app_label='dcim', model__in=['site', 'sitegroup', 'tenant']) |
        Q(app_label='tenancy', model__in=['tenant', 'tenantgroup'])
    )


def get_tables_to_replicate():
    tables = set()

    for model_id in MODELS_TO_REPLICATE:
        app_label, model_name = model_id.split('.')
        model = apps.get_model(app_label, model_name)

        # Capture the model's table
        tables.add(model._meta.db_table)

        # Capture any M2M fields which reference other replicated models
        for m2m_field in model._meta.local_many_to_many:
            related_model = m2m_field.related_model
            related_model_id = f'{related_model._meta.app_label}.{related_model._meta.model_name}'
            if related_model_id in MODELS_TO_REPLICATE:
                if hasattr(m2m_field, 'through'):
                    # Field is actually a manager
                    m2m_table = m2m_field.through._meta.db_table
                else:
                    m2m_table = m2m_field._get_m2m_db_table(model._meta)
                tables.add(m2m_table)

    return sorted(tables)
