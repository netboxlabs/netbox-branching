from netbox_vcs.models import Branch
from utilities.testing import ViewTestCases, create_tags


class BranchTestCase(ViewTestCases.PrimaryObjectViewTestCase):
    model = Branch

    def _get_base_url(self):
        viewname = super()._get_base_url()
        return f'plugins:{viewname}'

    @classmethod
    def setUpTestData(cls):

        branches = (
            Branch(name='Branch 1'),
            Branch(name='Branch 2'),
            Branch(name='Branch 3'),
        )
        Branch.objects.bulk_create(branches)

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        cls.form_data = {
            'name': 'Branch X',
            'description': 'Another branch',
            'tags': [t.pk for t in tags],
        }

        cls.csv_data = (
            "name,description",
            "Branch 4,Fourth branch",
            "Branch 5,Fifth branch",
            "Branch 6,Sixth branch",
        )

        cls.csv_update_data = (
            "id,description",
            f"{branches[0].pk},New description",
            f"{branches[1].pk},New description",
            f"{branches[2].pk},New description",
        )

        cls.bulk_edit_data = {
            'description': 'New description',
        }
