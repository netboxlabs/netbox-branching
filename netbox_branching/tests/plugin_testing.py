from utilities.testing import APIViewTestCases, ViewTestCases


class PluginViewTestCase:
    """Prepend the ``plugins:`` namespace when reversing UI view names."""

    def _get_base_url(self):
        viewname = super()._get_base_url()
        return f'plugins:{viewname}'


class PluginAPIViewTestCase:
    """Point the API test case at the plugin's ``plugins-api:`` namespace."""

    def _get_view_namespace(self):
        return f'plugins-api:{self.model._meta.app_label}-api'


class PluginTestCases:
    """Plugin-aware variants of NetBox-core's view test cases.

    NetBox plugin URLs live under the ``plugins:`` (UI) and
    ``plugins-api:`` (REST API) namespaces; ``PluginViewTestCase`` /
    ``PluginAPIViewTestCase`` route ``reverse()`` through the right
    namespace. Compose them with ``ViewTestCases.PrimaryObjectViewTestCase``
    so all standard primary-object views (Get / Edit / Delete / List /
    BulkEdit / BulkDelete / BulkImport) test cleanly.
    """

    class PrimaryObjectViewTestCase(
        PluginViewTestCase,
        ViewTestCases.PrimaryObjectViewTestCase,
    ):
        """Composite for first-class plugin models."""

        maxDiff = None


class PluginAPIViewTestCases:
    """Plugin-aware variants of the standard API view test cases.

    ``APIViewTestCases.APIViewTestCase`` (NetBox-core) covers Get / List /
    Create / Update / Delete / Bulk operations plus GraphQL. The composite
    below mirrors it but routes via the ``plugins-api:`` namespace.
    """

    class APIViewTestCase(
        PluginAPIViewTestCase,
        APIViewTestCases.GetObjectViewTestCase,
        APIViewTestCases.ListObjectsViewTestCase,
        APIViewTestCases.CreateObjectViewTestCase,
        APIViewTestCases.UpdateObjectViewTestCase,
        APIViewTestCases.DeleteObjectViewTestCase,
        APIViewTestCases.GraphQLTestCase,
    ):
        """Composite for first-class plugin models."""
