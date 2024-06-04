from django.utils.translation import gettext_lazy as _

from utilities.choices import ChoiceSet


class ContextStatusChoices(ChoiceSet):
    NEW = 'new'
    PROVISIONING = 'provisioning'
    REBASING = 'rebasing'
    READY = 'ready'
    FAILED = 'failed'

    CHOICES = (
        (NEW, _('New'), 'blue'),
        (PROVISIONING, _('Provisioning'), 'orange'),
        (REBASING, _('Rebasing'), 'cyan'),
        (READY, _('Ready'), 'green'),
        (FAILED, _('Failed'), 'red'),
    )
