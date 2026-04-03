from utilities.testing import TestCase

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.events import add_branch_context
from netbox_branching.models import Branch
from netbox_branching.utilities import activate_branch


class AddBranchContextTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch(name='Test Branch')
        cls.branch.status = BranchStatusChoices.READY
        cls.branch.save(provision=False)

    def test_no_branch_active(self):
        """Events are not modified when no branch is active."""
        event = {'data': {'display': 'Site 1', 'name': 'Site 1'}}
        add_branch_context([event])
        self.assertNotIn('active_branch', event['data'])
        self.assertEqual(event['data']['display'], 'Site 1')

    def test_branch_active_injects_context(self):
        """active_branch is injected into event data when a branch is active."""
        event = {'data': {'display': 'Site 1'}}
        with activate_branch(self.branch):
            add_branch_context([event])
        self.assertEqual(event['data']['active_branch'], {
            'id': self.branch.pk,
            'name': self.branch.name,
            'schema_id': self.branch.schema_id,
        })

    def test_branch_active_annotates_display(self):
        """The display field is annotated with the branch name when a branch is active."""
        event = {'data': {'display': 'Site 1'}}
        with activate_branch(self.branch):
            add_branch_context([event])
        self.assertEqual(event['data']['display'], f'Site 1 (branch: {self.branch.name})')

    def test_branch_active_no_display_field(self):
        """Events without a display field are handled without error."""
        event = {'data': {'name': 'Site 1'}}
        with activate_branch(self.branch):
            add_branch_context([event])
        self.assertEqual(event['data']['active_branch']['name'], self.branch.name)
        self.assertNotIn('display', event['data'])

    def test_multiple_events_all_enriched(self):
        """All events in the list are enriched when a branch is active."""
        events = [
            {'data': {'display': 'Site 1'}},
            {'data': {'display': 'Site 2'}},
        ]
        with activate_branch(self.branch):
            add_branch_context(events)
        for event in events:
            self.assertIn('active_branch', event['data'])
            self.assertIn(f'(branch: {self.branch.name})', event['data']['display'])
