"""
Tests for the ``register_branching_resolver`` plugin extension point.

The resolver list lives at module level on ``netbox_branching.utilities``;
each test snapshots and restores it so registrations don't leak between
tests.
"""
from contextlib import contextmanager

from django.contrib.auth.models import Group
from django.test import TestCase
from ipam.models import Prefix

from netbox_branching import utilities
from netbox_branching.utilities import (
    register_branching_resolver,
    supports_branching,
)


@contextmanager
def _isolated_resolvers():
    """Snapshot ``_branching_resolvers`` and restore it on exit."""
    saved = list(utilities._branching_resolvers)
    utilities._branching_resolvers.clear()
    try:
        yield
    finally:
        utilities._branching_resolvers.clear()
        utilities._branching_resolvers.extend(saved)


class RegisterBranchingResolverTestCase(TestCase):
    """``register_branching_resolver`` input validation."""

    def test_register_callable(self):
        with _isolated_resolvers():
            def resolver(model):
                return None
            register_branching_resolver(resolver)
            self.assertIn(resolver, utilities._branching_resolvers)

    def test_register_non_callable_raises(self):
        with _isolated_resolvers():
            with self.assertRaises(TypeError):
                register_branching_resolver('not callable')
            with self.assertRaises(TypeError):
                register_branching_resolver(42)
            with self.assertRaises(TypeError):
                register_branching_resolver(None)
            self.assertEqual(utilities._branching_resolvers, [])

    def test_register_lambda(self):
        with _isolated_resolvers():
            register_branching_resolver(lambda model: None)
            self.assertEqual(len(utilities._branching_resolvers), 1)

    def test_multiple_registrations_preserve_order(self):
        with _isolated_resolvers():
            r1 = lambda model: None  # noqa: E731
            r2 = lambda model: None  # noqa: E731
            r3 = lambda model: None  # noqa: E731
            register_branching_resolver(r1)
            register_branching_resolver(r2)
            register_branching_resolver(r3)
            self.assertEqual(utilities._branching_resolvers, [r1, r2, r3])


class ResolverDispatchTestCase(TestCase):
    """``supports_branching`` integration with registered resolvers."""

    def test_resolver_returning_true_opts_model_in(self):
        """A resolver returning True branches a model that wouldn't be by default.

        ``auth.Group`` is not a ChangeLoggingMixin subclass and not in
        INCLUDE_MODELS, so supports_branching returns False by default.  A
        resolver returning True should flip that result.
        """
        # Baseline: not branchable without resolver
        self.assertFalse(supports_branching(Group))

        with _isolated_resolvers():
            def resolver(model):
                if model is Group:
                    return True
                return None
            register_branching_resolver(resolver)
            self.assertTrue(supports_branching(Group))

    def test_resolver_returning_false_opts_model_out(self):
        """A resolver returning False excludes a model that's branchable by default.

        ``ipam.Prefix`` is a ChangeLoggingMixin subclass so supports_branching
        returns True by default.  A resolver returning False should override
        that decision.
        """
        # Baseline: branchable without resolver
        self.assertTrue(supports_branching(Prefix))

        with _isolated_resolvers():
            def resolver(model):
                if model is Prefix:
                    return False
                return None
            register_branching_resolver(resolver)
            self.assertFalse(supports_branching(Prefix))

    def test_resolver_returning_none_falls_through(self):
        """A resolver returning None should not affect the default decision."""
        with _isolated_resolvers():
            def resolver(model):
                return None
            register_branching_resolver(resolver)
            # ChangeLoggingMixin-based model still branchable
            self.assertTrue(supports_branching(Prefix))
            # Non-ChangeLoggingMixin model still excluded
            self.assertFalse(supports_branching(Group))

    def test_first_non_none_wins(self):
        """Resolvers run in registration order; first non-None decides."""
        with _isolated_resolvers():
            register_branching_resolver(lambda m: None)         # defers
            register_branching_resolver(lambda m: True)         # decides
            register_branching_resolver(lambda m: False)        # never reached
            self.assertTrue(supports_branching(Group))

    def test_raising_resolver_is_swallowed(self):
        """A resolver that raises is logged and treated as None."""
        def bad_resolver(model):
            raise RuntimeError('boom')

        with _isolated_resolvers():
            register_branching_resolver(bad_resolver)
            with self.assertLogs('netbox_branching.utilities', level='ERROR') as cm:
                # Falls through to default heuristic.
                self.assertTrue(supports_branching(Prefix))
                self.assertFalse(supports_branching(Group))
            joined = '\n'.join(cm.output)
            self.assertIn('boom', joined)
            self.assertIn('treating as None', joined)

    def test_raising_resolver_does_not_block_subsequent_resolvers(self):
        """A raising resolver should not prevent the next resolver from running."""
        def bad_resolver(model):
            raise RuntimeError('boom')

        with _isolated_resolvers():
            register_branching_resolver(bad_resolver)
            register_branching_resolver(lambda m: True if m is Group else None)
            with self.assertLogs('netbox_branching.utilities', level='ERROR'):
                self.assertTrue(supports_branching(Group))

    def test_include_models_takes_precedence_over_resolver(self):
        """``INCLUDE_MODELS`` runs before resolvers; a False resolver can't override it.

        ``extras.taggeditem`` is in INCLUDE_MODELS, so even a resolver returning
        False should not exclude it.
        """
        from extras.models import TaggedItem
        with _isolated_resolvers():
            register_branching_resolver(lambda m: False)
            self.assertTrue(supports_branching(TaggedItem))

    def test_exempt_models_filter_still_runs_after_resolver(self):
        """A resolver returning True is still subject to ``exempt_models``."""
        from django.test import override_settings

        with _isolated_resolvers():
            register_branching_resolver(lambda m: True if m is Group else None)
            # Without exempt_models: resolver opts Group in.
            self.assertTrue(supports_branching(Group))
            # With exempt_models matching Group: exempt wins.
            with override_settings(PLUGINS_CONFIG={
                'netbox_branching': {'exempt_models': ['auth.group']},
            }):
                self.assertFalse(supports_branching(Group))
