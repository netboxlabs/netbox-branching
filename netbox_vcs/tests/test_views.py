from netbox_vcs.models import Context
from utilities.testing import ViewTestCases, create_tags


class ContextTestCase(ViewTestCases.PrimaryObjectViewTestCase):
    model = Context

    def _get_base_url(self):
        viewname = super()._get_base_url()
        return f'plugins:{viewname}'

    @classmethod
    def setUpTestData(cls):

        contexts = (
            Context(name='Context 1'),
            Context(name='Context 2'),
            Context(name='Context 3'),
        )
        Context.objects.bulk_create(contexts)

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        cls.form_data = {
            'name': 'Context X',
            'description': 'Another context',
            'tags': [t.pk for t in tags],
        }

        cls.csv_data = (
            "name,description",
            "Context 4,Fourth context",
            "Context 5,Fifth context",
            "Context 6,Sixth context",
        )

        cls.csv_update_data = (
            "id,description",
            f"{contexts[0].pk},New description",
            f"{contexts[1].pk},New description",
            f"{contexts[2].pk},New description",
        )

        cls.bulk_edit_data = {
            'description': 'New description',
        }
