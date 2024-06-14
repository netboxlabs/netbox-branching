from django.utils.translation import gettext_lazy as _

from utilities.choices import ChoiceSet


class BranchStatusChoices(ChoiceSet):
    NEW = 'new'
    PROVISIONING = 'provisioning'
    REBASING = 'rebasing'
    READY = 'ready'
    APPLIED = 'applied'
    FAILED = 'failed'

    CHOICES = (
        (NEW, _('New'), 'cyan'),
        (PROVISIONING, _('Provisioning'), 'orange'),
        (REBASING, _('Rebasing'), 'orange'),
        (READY, _('Ready'), 'green'),
        (APPLIED, _('Applied'), 'blue'),
        (FAILED, _('Failed'), 'red'),
    )
