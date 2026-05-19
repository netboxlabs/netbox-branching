"""
Tests for the ``Model.resolve_field_aliases`` opt-in classmethod hook and
the ``compute_conflicts`` public utility.

The hook is called from one place inside netbox-branching:

* ``netbox_branching.utilities.update_object`` — once at the top, before the
  per-attribute apply loop runs.  Lets plugins translate stale ``ObjectChange``
  keys (e.g. after a field rename) to current attribute names so the apply
  loop can match them against ``instance._meta.get_field(attr)``.

Plugins that also need ``ChangeDiff.conflicts`` recomputed against normalized
dicts should connect a ``post_save`` receiver on ``ChangeDiff`` and call
``netbox_branching.models.changes.compute_conflicts`` themselves — the
algorithm is exposed publicly for that purpose and is unit-tested below.
"""
from unittest.mock import patch

from django.test import TestCase
from ipam.models import Prefix

from netbox_branching.models.changes import compute_conflicts
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


class ComputeConflictsTests(TestCase):
    """``compute_conflicts`` is a pure function exposing branching's 3-way diff algorithm."""

    UPDATE = 'update'
    DELETE = 'delete'

    def test_original_none_returns_none(self):
        self.assertIsNone(compute_conflicts(self.UPDATE, None, {'a': 1}, {'a': 1}))

    def test_update_no_conflicts_when_three_dicts_agree(self):
        # original=a, modified=b, current=a → branch changed, main untouched → not a conflict
        self.assertIsNone(compute_conflicts(
            self.UPDATE, {'k': 'a'}, {'k': 'b'}, {'k': 'a'},
        ))

    def test_update_conflict_when_modified_and_current_diverge(self):
        # original=a, modified=b, current=c → both sides changed differently → conflict
        self.assertEqual(
            compute_conflicts(self.UPDATE, {'k': 'a'}, {'k': 'b'}, {'k': 'c'}),
            ['k'],
        )

    def test_update_current_none_flags_all_branch_changes(self):
        # Object deleted in main; every branch modification is a conflict.
        self.assertEqual(
            compute_conflicts(self.UPDATE, {'a': 1, 'b': 2}, {'a': 1, 'b': 99}, None),
            ['b'],
        )

    def test_update_divergent_keys_do_not_raise(self):
        """A key present in ``original`` but missing from ``modified`` no longer KeyErrors."""
        # original={old:'a'}, modified={new:'b'}, current=None
        # Pre-fix this raised KeyError on modified['old'].  Now: old != None → conflict.
        self.assertEqual(
            compute_conflicts(self.UPDATE, {'old': 'a'}, {'new': 'b'}, None),
            ['old'],
        )

    def test_update_divergent_keys_with_current(self):
        # Verifies the 3-way branch also tolerates divergent keys via .get().
        self.assertEqual(
            compute_conflicts(
                self.UPDATE,
                {'old': 'a'}, {'new': 'b'}, {'old': 'a'},
            ),
            ['old'],
        )

    def test_delete_current_none_returns_none(self):
        # Deleted on both sides → not a conflict.
        self.assertIsNone(compute_conflicts(self.DELETE, {'k': 'a'}, {}, None))

    def test_delete_conflict_when_main_modified(self):
        # Branch deleted; main modified → conflict.
        self.assertEqual(
            compute_conflicts(self.DELETE, {'k': 'a'}, {}, {'k': 'b'}),
            ['k'],
        )

    def test_empty_conflict_list_returns_none(self):
        # Result of [] (no conflicts) should normalize to None to match the
        # ChangeDiff.conflicts NULL-when-clean convention.
        self.assertIsNone(compute_conflicts(self.UPDATE, {'k': 'a'}, {'k': 'a'}, {'k': 'a'}))
