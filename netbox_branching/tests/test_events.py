import uuid

import django_rq
from core.events import OBJECT_CREATED
from core.models import ObjectType
from dcim.models import Site
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.urls import reverse
from extras.choices import EventRuleActionChoices
from extras.events import enqueue_event, flush_events
from extras.models import EventRule, Webhook

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import Branch

User = get_user_model()

ENRICHED_PIPELINE = [
    'netbox_branching.events.add_branch_context',
    'extras.events.process_event_queue',
]


class AddBranchContextTestCase(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', is_superuser=True)

        self.branch = Branch(name='Test Branch')
        self.branch.status = BranchStatusChoices.READY
        self.branch.save(provision=False)

        self.queue = django_rq.get_queue('default')
        self.queue.empty()

        self.request = RequestFactory().get(reverse('home'))
        self.request.id = uuid.uuid4()
        self.request.user = self.user

        self.site_type = ObjectType.objects.get_for_model(Site)

    def _enqueue_site_event(self, site):
        queue = {}
        enqueue_event(queue, instance=site, request=self.request, event_type=OBJECT_CREATED)
        return list(queue.values())

    def _make_webhook_rule(self):
        webhook = Webhook.objects.create(name='Test Webhook', payload_url='http://localhost/')
        webhook_type = ObjectType.objects.get_for_model(Webhook)
        rule = EventRule.objects.create(
            name='Test Rule',
            event_types=[OBJECT_CREATED],
            action_type=EventRuleActionChoices.WEBHOOK,
            action_object_type=webhook_type,
            action_object_id=webhook.pk,
        )
        rule.object_types.set([self.site_type])
        return rule

    @override_settings(EVENTS_PIPELINE=ENRICHED_PIPELINE)
    def test_branch_active_injects_context(self):
        """Webhook job data includes active_branch when a branch is active during flush_events."""
        self._make_webhook_rule()
        site = Site.objects.create(name='Site 1', slug='site-1')
        self.request.active_branch = self.branch
        events = self._enqueue_site_event(site)

        flush_events(events)

        self.assertEqual(self.queue.count, 1)
        data = self.queue.jobs[0].kwargs['data']
        self.assertEqual(data['active_branch'], {
            'id': self.branch.pk,
            'name': self.branch.name,
            'schema_id': self.branch.schema_id,
        })

    @override_settings(EVENTS_PIPELINE=ENRICHED_PIPELINE)
    def test_no_branch_active_no_enrichment(self):
        """Event data is not modified when no branch is active during flush_events."""
        self._make_webhook_rule()
        site = Site.objects.create(name='Site 1', slug='site-1')
        events = self._enqueue_site_event(site)
        flush_events(events)

        self.assertEqual(self.queue.count, 1)
        data = self.queue.jobs[0].kwargs['data']
        self.assertIsNone(data.get('active_branch'))
