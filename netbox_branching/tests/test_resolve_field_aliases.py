"""
Tests for the ``Model.resolve_field_aliases`` opt-in classmethod hook.

The hook is called from two places in netbox-branching:

* ``netbox_branching.utilities.update_object`` — once at the top, before the
  per-attribute apply loop runs.
* ``netbox_branching.models.changes.ChangeDiff._update_conflicts`` — once on
  each of ``original`` / ``modified`` / ``current`` before the conflict
  comparison.

These tests verify both call sites: that the hook is invoked when present, is
skipped when absent, and that its return value (rather than the original data)
is what subsequent logic sees.
"""
from unittest.mock import patch

from django.test import TestCase
from ipam.models import Prefix

from netbox_branching.models.changes import ChangeDiff
from netbox_branching.utilities import update_object


class UpdateObjectResolveAliasesTests(TestCase):
    """``update_object`` consults ``type(instance).resolve_field_aliases`` when defined."""

    def _make_prefix(self, **kwargs):
        """Create + save a Prefix.  ``instance.snapshot()`` requires a PK
        because it touches the taggit reverse manager."""
        return Prefix.objects.create(prefix='10.0.0.0/24', **kwargs)

    def test_hook_absent_uses_data_asis(self):
        """A model without ``resolve_field_aliases`` receives the raw data dict."""
        prefix = self._make_prefix()
        update_object(prefix, {'description': 'set by test'}, using='default')
        self.assertEqual(prefix.description, 'set by test')
        # And confirm: Prefix has no resolve_field_aliases classmethod
        self.assertFalse(hasattr(Prefix, 'resolve_field_aliases'))

    def test_hook_present_data_is_transformed(self):
        """When ``resolve_field_aliases`` is defined, ``update_object`` uses its return value."""

        def fake_resolve(data):
            # Rewrite 'old_desc' → 'description'
            return {('description' if k == 'old_desc' else k): v for k, v in data.items()}

        with patch.object(Prefix, 'resolve_field_aliases', staticmethod(fake_resolve), create=True):
            prefix = self._make_prefix()
            update_object(prefix, {'old_desc': 'translated value'}, using='default')

        self.assertEqual(prefix.description, 'translated value')

    def test_hook_can_drop_keys(self):
        """A resolve_field_aliases that returns a smaller dict drops the omitted keys."""

        def drop_all(data):
            return {}

        with patch.object(Prefix, 'resolve_field_aliases', staticmethod(drop_all), create=True):
            prefix = self._make_prefix(description='original')
            update_object(prefix, {'description': 'would-be new value'}, using='default')

        # The applied data was empty, so description is unchanged from the instance state.
        self.assertEqual(prefix.description, 'original')

    def test_hook_raising_propagates(self):
        """A buggy resolve_field_aliases should surface, not be silently swallowed."""

        def bad(data):
            raise RuntimeError('boom')

        with patch.object(Prefix, 'resolve_field_aliases', staticmethod(bad), create=True):
            prefix = self._make_prefix()
            with self.assertRaises(RuntimeError):
                update_object(prefix, {'description': 'x'}, using='default')


class ChangeDiffResolveAliasesTests(TestCase):
    """``ChangeDiff._update_conflicts`` consults the hook before comparing dicts."""

    def _make_diff(self, action='update', original=None, modified=None, current=None):
        """Build a ChangeDiff instance in memory (no save).

        ``_update_conflicts`` is called by ``save()`` but is itself a pure
        method that reads only the dict fields and the action.  Constructing
        a non-persisted instance is enough to exercise it directly.
        """
        # Use a Prefix ObjectType — the actual model only matters when
        # resolve_field_aliases is patched onto it.
        from core.models import ObjectType
        diff = ChangeDiff(
            action=action,
            original=original,
            modified=modified,
            current=current,
            object_type=ObjectType.objects.get_for_model(Prefix),
        )
        return diff

    def test_hook_absent_uses_dicts_asis(self):
        """Without ``resolve_field_aliases``, comparison runs against the raw dicts."""
        diff = self._make_diff(
            original={'description': 'a'},
            modified={'description': 'b'},
            current={'description': 'a'},
        )
        diff._update_conflicts()
        # original=a, modified=b, current=a:
        #   a != b  (True), a != a (False)  → not a conflict
        self.assertIsNone(diff.conflicts)

    def test_hook_invoked_when_present(self):
        """``_update_conflicts`` calls ``resolve_field_aliases`` for each of the three dicts."""
        called_with = []

        def trace(data):
            called_with.append(data)
            return data

        with patch.object(Prefix, 'resolve_field_aliases', staticmethod(trace), create=True):
            diff = self._make_diff(
                original={'a': 1},
                modified={'a': 2},
                current={'a': 1},
            )
            diff._update_conflicts()

        # Should have been called for original, modified, and current.
        self.assertEqual(len(called_with), 3)
        self.assertEqual(called_with[0], {'a': 1})  # original
        self.assertEqual(called_with[1], {'a': 2})  # modified
        self.assertEqual(called_with[2], {'a': 1})  # current

    def test_alias_resolution_aligns_divergent_keys(self):
        """A rename-style resolver makes a previously-divergent key set match."""

        def resolve_aliases(data):
            # Rewrite 'old_name' → 'new_name'.
            return {('new_name' if k == 'old_name' else k): v for k, v in data.items()}

        with patch.object(Prefix, 'resolve_field_aliases', staticmethod(resolve_aliases), create=True):
            diff = self._make_diff(
                original={'old_name': 'a'},     # pre-rename snapshot
                modified={'new_name': 'b'},     # post-rename snapshot
                current={'old_name': 'a'},      # main's view (rename not applied yet)
            )
            # After alias resolution all three become {'new_name': ...}.
            # Without the hook this would raise KeyError on `modified['old_name']`.
            diff._update_conflicts()

        # original=a, modified=b, current=a → 3-way comparison: not a conflict.
        self.assertIsNone(diff.conflicts)

    def test_current_none_uses_modified_directly(self):
        """When current is None (object deleted in main), comparison uses original vs modified only."""
        diff = self._make_diff(
            action='update',
            original={'description': 'a'},
            modified={'description': 'b'},
            current=None,
        )
        diff._update_conflicts()
        # All branch modifications are conflicts when current is None.
        self.assertEqual(diff.conflicts, ['description'])

    def test_hook_raising_propagates_in_conflict_detection(self):
        """A buggy resolve_field_aliases surfaces from _update_conflicts too."""

        def bad(data):
            raise RuntimeError('boom')

        with patch.object(Prefix, 'resolve_field_aliases', staticmethod(bad), create=True):
            diff = self._make_diff(
                original={'a': 1},
                modified={'a': 2},
                current={'a': 1},
            )
            with self.assertRaises(RuntimeError):
                diff._update_conflicts()
