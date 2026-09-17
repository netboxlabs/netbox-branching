import uuid
from unittest import mock

import django_rq
from core.events import OBJECT_CREATED
from core.models import ObjectType
from dcim.models import Site
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import RequestFactory, override_settings
from django.urls import reverse
from extras.choices import EventRuleActionChoices
from extras.events import enqueue_event, flush_events
from extras.models import EventRule, Webhook

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.events import BRANCH_DEPROVISIONED
from netbox_branching.models import Branch
from netbox_branching.tests.utils import FastTeardownTransactionTestCase

User = get_user_model()

ENRICHED_PIPELINE = [
    'netbox_branching.events.add_branch_context',
    'extras.events.process_event_queue',
]


class AddBranchContextTestCase(FastTeardownTransactionTestCase):
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

    @override_settings(EVENTS_PIPELINE=ENRICHED_PIPELINE)
    def test_no_request_in_event(self):
        """active_branch is None when the event carries no request (e.g. background-triggered events)."""
        self._make_webhook_rule()
        site = Site.objects.create(name='Site 1', slug='site-1')
        events = self._enqueue_site_event(site)
        for event in events:
            event.pop('request', None)
        flush_events(events)

        self.assertEqual(self.queue.count, 1)
        data = self.queue.jobs[0].kwargs['data']
        self.assertIsNone(data.get('active_branch'))


class BranchDeprovisionedEventRuleTestCase(FastTeardownTransactionTestCase):
    """
    Regression tests for issue #641.

    When an enabled EventRule subscribes to `branch_deprovisioned`, deleting a
    branch used to raise `ValueError: Branch objects need to have a primary key
    value before you can access their tags.` — `Branch.delete()` ran
    `super().delete()` first, Django's collector nulled the instance pk, and the
    `post_deprovision` receiver then tried to serialize a pk-less instance. The
    500 rolled the whole atomic block back, so the branch survived and could
    never be deleted while the rule existed.
    """
    serialized_rollback = True

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', is_superuser=True)
        self.queue = django_rq.get_queue('default')
        self.queue.empty()

    def tearDown(self):
        # Leftover webhook jobs would inflate the queue-depth assertions in other classes.
        self.queue.empty()
        for branch in Branch.objects.all():
            if hasattr(connections._connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _make_branch_event_rule(self, event_type):
        webhook = Webhook.objects.create(name='Test Webhook', payload_url='http://localhost/')
        rule = EventRule.objects.create(
            name='Branch Rule',
            event_types=[event_type],
            enabled=True,
            action_type=EventRuleActionChoices.WEBHOOK,
            action_object_type=ObjectType.objects.get_for_model(Webhook),
            action_object_id=webhook.pk,
        )
        rule.object_types.set([ObjectType.objects.get_by_natural_key('netbox_branching', 'branch')])
        return rule

    def test_delete_succeeds_with_deprovision_event_rule(self):
        self._make_branch_event_rule(BRANCH_DEPROVISIONED)

        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)
        branch_pk, schema_name = branch.pk, branch.schema_name

        branch.delete()

        self.assertFalse(Branch.objects.filter(pk=branch_pk).exists())
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s",
                [schema_name]
            )
            self.assertIsNone(cursor.fetchone(), msg="Schema was not dropped")

    def test_deprovision_event_payload_identifies_the_branch(self):
        """
        The event payload must carry the deleted branch's identity — a receiver that
        cannot tell which branch went away is no more useful than the 500 was.

        Asserted at the point the payload is handed to NetBox rather than by reading
        the RQ job, whose shape varies across NetBox versions.
        """
        self._make_branch_event_rule(BRANCH_DEPROVISIONED)

        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(user=None)
        branch_pk, schema_id = branch.pk, branch.schema_id

        with mock.patch('netbox_branching.signal_receivers.process_event_rules') as mock_process:
            branch.delete()

        self.assertEqual(
            mock_process.call_count, 1,
            msg="Expected exactly one dispatch; 0 means the EventRule did not match the event type"
        )
        kwargs = mock_process.call_args.kwargs
        # NetBox 4.5.2+ nests the payload under `event`; older versions pass it flat.
        data = kwargs['event']['data'] if 'event' in kwargs else kwargs['data']
        self.assertEqual(data['id'], branch_pk)
        self.assertEqual(data['name'], 'Branch 1')
        self.assertEqual(data['schema_id'], schema_id)
