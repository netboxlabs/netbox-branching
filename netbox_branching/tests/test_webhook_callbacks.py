"""
Tests for `netbox_branching.webhook_callbacks.set_active_branch`.

This callback is registered with NetBox's webhook system so every outbound
webhook payload includes the active branch context. Without it, integrations
that act on NetBox events have no way to tell which branch a change came from,
and may apply branch-only changes back to main.
"""

from unittest import mock

from django.urls import reverse

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import Branch
from netbox_branching.webhook_callbacks import set_active_branch
from utilities.testing import TestCase

from .utils import fake_request


class SetActiveBranchTestCase(TestCase):
    def test_returns_none_when_request_is_missing(self):
        """
        Webhooks fired outside an HTTP request (e.g. from a background job
        that didn't carry a request) get no active-branch context attached.
        """
        result = set_active_branch(object_type=None, event_type=None, data=None, request=None)
        self.assertIsNone(result)

    @mock.patch('netbox_branching.webhook_callbacks.get_active_branch', return_value=None)
    def test_returns_null_active_branch_when_no_branch_is_active(self, _mock):
        """Webhook payload still includes the active_branch key so integrators
        can rely on its presence; value is None when the request had no branch."""
        result = set_active_branch(object_type=None, event_type=None, data=None, request=mock.Mock())
        self.assertEqual(result, {'active_branch': None})

    def test_returns_branch_attrs_when_branch_is_active(self):
        branch = Branch(name='Webhook Branch')
        branch.save(provision=False)
        with mock.patch(
            'netbox_branching.webhook_callbacks.get_active_branch',
            return_value=branch,
        ):
            result = set_active_branch(object_type=None, event_type=None, data=None, request=mock.Mock())
        self.assertEqual(
            result,
            {
                'active_branch': {
                    'id': branch.pk,
                    'name': 'Webhook Branch',
                    'schema_id': branch.schema_id,
                },
            },
        )

    def test_returns_branch_attrs_for_fake_api_request_with_header(self):
        """
        Webhooks for REST API changes are rendered in a worker from a NetBoxFakeRequest, which
        has no `headers` attribute. The X-NetBox-Branch header must still identify the branch.
        See #675.
        """
        branch = Branch(name='Webhook Branch')
        branch.status = BranchStatusChoices.READY  # Fake provisioning
        branch.save(provision=False)
        request = fake_request(self.user, reverse('dcim-api:site-list'), headers={'x-netbox-branch': branch.schema_id})

        result = set_active_branch(object_type=None, event_type=None, data=None, request=request)

        self.assertEqual(result['active_branch']['schema_id'], branch.schema_id)
