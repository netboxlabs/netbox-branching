# Temporary placeholder for stuff we still need to build
from collections import defaultdict

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

MODELS_TO_REPLICATE = (
    'circuits.circuit',
    'circuits.circuittermination',
    'circuits.circuittype',
    'circuits.provider',
    'circuits.provideraccount',
    'circuits.providernetwork',
    'dcim.cable',
    'dcim.cabletermination',
    'dcim.consoleport',
    'dcim.consoleporttemplate',
    'dcim.consoleserverport',
    'dcim.consoleserverporttemplate',
    'dcim.device',
    'dcim.devicebay',
    'dcim.devicebaytemplate',
    'dcim.devicerole',
    'dcim.devicetype',
    'dcim.frontport',
    'dcim.frontporttemplate',
    'dcim.interface',
    'dcim.interfacetemplate',
    'dcim.inventoryitem',
    'dcim.inventoryitemrole',
    'dcim.inventoryitemtemplate',
    'dcim.location',
    'dcim.manufacturer',
    'dcim.module',
    'dcim.modulebay',
    'dcim.modulebaytemplate',
    'dcim.moduletype',
    'dcim.platform',
    'dcim.powerfeed',
    'dcim.poweroutlet',
    'dcim.poweroutlettemplate',
    'dcim.powerpanel',
    'dcim.powerport',
    'dcim.powerporttemplate',
    'dcim.rack',
    'dcim.rackreservation',
    'dcim.rackrole',
    'dcim.rearport',
    'dcim.rearporttemplate',
    'dcim.region',
    'dcim.site',
    'dcim.sitegroup',
    'dcim.virtualchassis',
    'dcim.virtualdevicecontext',
    'ipam.aggregate',
    'ipam.asn',
    'ipam.asnrange',
    'ipam.fhrpgroup',
    'ipam.fhrpgroupassignment',
    'ipam.ipaddress',
    'ipam.iprange',
    'ipam.prefix',
    'ipam.rir',
    'ipam.role',
    'ipam.routetarget',
    'ipam.service',
    'ipam.servicetemplate',
    'ipam.vlan',
    'ipam.vlangroup',
    'ipam.vrf',
    'extras.configcontext',
    'extras.configtemplate',
    'extras.imageattachment',
    'extras.journalentry',
    'extras.tag',
    'tenancy.contact',
    'tenancy.contactassignment',
    'tenancy.contactgroup',
    'tenancy.contactrole',
    'tenancy.tenant',
    'tenancy.tenantgroup',
    'virtualization.cluster',
    'virtualization.clustergroup',
    'virtualization.clustertype',
    'virtualization.virtualdisk',
    'virtualization.virtualmachine',
    'virtualization.vminterface',
    'vpn.ikepolicy',
    'vpn.ikeproposal',
    'vpn.ipsecpolicy',
    'vpn.ipsecprofile',
    'vpn.ipsecproposal',
    'vpn.l2vpn',
    'vpn.l2vpntermination',
    'vpn.tunnel',
    'vpn.tunnelgroup',
    'vpn.tunneltermination',
)


# TODO: Source app labels & model names from MODELS_TO_REPLICATE
def get_relevant_content_types():
    model_map = defaultdict(set)

    for model_name in MODELS_TO_REPLICATE:
        app_label, model = model_name.split('.')
        model_map[app_label].add(model)

    q = Q()
    for app_label, models in model_map.items():
        q |= Q(app_label=app_label, model__in=models)

    return ContentType.objects.filter(q)


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
