"""
Tests for the ``register_objectchange_field_migrator`` plugin extension point.

Migrators are consulted in two places by netbox-branching:

* ``netbox_branching.utilities.update_object`` — once at the top, before the
  per-attribute apply loop runs.
* ``netbox_branching.models.changes.ChangeDiff._update_conflicts`` — once on
  each of ``original`` / ``modified`` / ``current`` before the conflict
  comparison.

These tests verify both call sites: that a registered migrator is consulted,
that an unregistered one is not, that the first non-``None`` return wins, and
that the (possibly translated) data is what subsequent logic sees.
"""
from contextlib import contextmanager

from django.test import TestCase
from ipam.models import Prefix

from netbox_branching import utilities
from netbox_branching.models.changes import ChangeDiff
from netbox_branching.utilities import (
    register_objectchange_field_migrator,
    update_object,
)


@contextmanager
def _isolated_registry():
    """Snapshot ``_objectchange_field_migrators`` and restore it on exit."""
    saved = list(utilities._objectchange_field_migrators)
    utilities._objectchange_field_migrators.clear()
    try:
        yield
    finally:
        utilities._objectchange_field_migrators.clear()
        utilities._objectchange_field_migrators.extend(saved)


class RegisterObjectChangeFieldMigratorTests(TestCase):
    """``register_objectchange_field_migrator`` input validation."""

    def test_appends_callable(self):
        with _isolated_registry():
            def migrator(model, data):
                return None

            register_objectchange_field_migrator(migrator)
            self.assertIn(migrator, utilities._objectchange_field_migrators)

    def test_rejects_non_callable(self):
        with _isolated_registry():
            with self.assertRaises(TypeError):
                register_objectchange_field_migrator('not callable')
            with self.assertRaises(TypeError):
                register_objectchange_field_migrator(42)
            with self.assertRaises(TypeError):
                register_objectchange_field_migrator(None)
            self.assertEqual(utilities._objectchange_field_migrators, [])

    def test_preserves_registration_order(self):
        with _isolated_registry():
            def m1(model, data): return None
            def m2(model, data): return None
            def m3(model, data): return None

            register_objectchange_field_migrator(m1)
            register_objectchange_field_migrator(m2)
            register_objectchange_field_migrator(m3)
            self.assertEqual(
                utilities._objectchange_field_migrators, [m1, m2, m3]
            )


class UpdateObjectMigratorTests(TestCase):
    """``update_object`` consults registered migrators."""

    def _make_prefix(self, **kwargs):
        """Create + save a Prefix.  ``instance.snapshot()`` requires a PK
        because it touches the taggit reverse manager."""
        return Prefix.objects.create(prefix='10.0.0.0/24', **kwargs)

    def test_no_migrator_uses_data_asis(self):
        """With no registered migrator, the raw data dict is applied."""
        with _isolated_registry():
            prefix = self._make_prefix()
            update_object(prefix, {'description': 'set by test'}, using='default')
        self.assertEqual(prefix.description, 'set by test')

    def test_migrator_transforms_data(self):
        """A registered migrator's return value replaces ``data``."""
        with _isolated_registry():
            def rename_migrator(model, data):
                if model is not Prefix:
                    return None
                return {
                    ('description' if k == 'old_desc' else k): v
                    for k, v in data.items()
                }

            register_objectchange_field_migrator(rename_migrator)
            prefix = self._make_prefix()
            update_object(
                prefix, {'old_desc': 'translated value'}, using='default'
            )

        self.assertEqual(prefix.description, 'translated value')

    def test_first_non_none_wins(self):
        """When multiple migrators are registered, the first non-None wins."""
        with _isolated_registry():
            def defer(model, data):
                return None

            def claim(model, data):
                return {'description': 'from claim'}

            def never_called(model, data):
                raise AssertionError('should not be called after a non-None')

            register_objectchange_field_migrator(defer)
            register_objectchange_field_migrator(claim)
            register_objectchange_field_migrator(never_called)

            prefix = self._make_prefix()
            update_object(prefix, {'description': 'original'}, using='default')

        self.assertEqual(prefix.description, 'from claim')

    def test_migrator_can_drop_keys(self):
        """A migrator that returns a smaller dict drops the omitted keys."""
        with _isolated_registry():
            def drop_all(model, data):
                return {}

            register_objectchange_field_migrator(drop_all)
            prefix = self._make_prefix(description='original')
            update_object(
                prefix, {'description': 'would-be new value'}, using='default'
            )

        self.assertEqual(prefix.description, 'original')

    def test_raising_migrator_treated_as_none(self):
        """A buggy migrator is logged + skipped; later migrators still run."""
        with _isolated_registry():
            def bad(model, data):
                raise RuntimeError('boom')

            def good(model, data):
                return {'description': 'from good'}

            register_objectchange_field_migrator(bad)
            register_objectchange_field_migrator(good)

            prefix = self._make_prefix()
            with self.assertLogs(
                'netbox_branching.utilities', level='ERROR'
            ) as cm:
                update_object(
                    prefix, {'description': 'original'}, using='default'
                )

        self.assertEqual(prefix.description, 'from good')
        self.assertTrue(any('boom' in m for m in cm.output))


class ChangeDiffMigratorTests(TestCase):
    """``ChangeDiff._update_conflicts`` consults registered migrators."""

    def _make_diff(self, action='update', original=None, modified=None, current=None):
        """Build a ChangeDiff instance in memory (no save)."""
        from core.models import ObjectType
        return ChangeDiff(
            action=action,
            original=original,
            modified=modified,
            current=current,
            object_type=ObjectType.objects.get_for_model(Prefix),
        )

    def test_no_migrator_uses_dicts_asis(self):
        """Without a registered migrator, comparison runs against the raw dicts."""
        with _isolated_registry():
            diff = self._make_diff(
                original={'description': 'a'},
                modified={'description': 'b'},
                current={'description': 'a'},
            )
            diff._update_conflicts()
        # original=a, modified=b, current=a → not a conflict.
        self.assertIsNone(diff.conflicts)

    def test_migrator_invoked_for_each_snapshot(self):
        """The migrator is called for original, modified, and current."""
        called_with = []

        with _isolated_registry():
            def trace(model, data):
                called_with.append(data)
                return data

            register_objectchange_field_migrator(trace)
            diff = self._make_diff(
                original={'a': 1},
                modified={'a': 2},
                current={'a': 1},
            )
            diff._update_conflicts()

        # Should have been called for original, modified, and current.
        self.assertEqual(len(called_with), 3)
        self.assertEqual(called_with[0], {'a': 1})
        self.assertEqual(called_with[1], {'a': 2})
        self.assertEqual(called_with[2], {'a': 1})

    def test_alias_translation_aligns_divergent_keys(self):
        """A rename-style migrator makes a previously-divergent key set match."""
        with _isolated_registry():
            def rename(model, data):
                return {
                    ('new_name' if k == 'old_name' else k): v
                    for k, v in data.items()
                }

            register_objectchange_field_migrator(rename)
            diff = self._make_diff(
                original={'old_name': 'a'},
                modified={'new_name': 'b'},
                current={'old_name': 'a'},
            )
            diff._update_conflicts()

        # After translation, all three become {'new_name': ...} → not a conflict.
        self.assertIsNone(diff.conflicts)

    def test_current_none_uses_modified_directly(self):
        """When current is None (object deleted in main), original vs modified only."""
        with _isolated_registry():
            diff = self._make_diff(
                action='update',
                original={'description': 'a'},
                modified={'description': 'b'},
                current=None,
            )
            diff._update_conflicts()
        self.assertEqual(diff.conflicts, ['description'])

    def test_raising_migrator_treated_as_none(self):
        """A buggy migrator is logged + skipped in conflict detection too."""
        with _isolated_registry():
            def bad(model, data):
                raise RuntimeError('boom')

            register_objectchange_field_migrator(bad)
            diff = self._make_diff(
                original={'a': 1},
                modified={'a': 2},
                current={'a': 1},
            )
            with self.assertLogs(
                'netbox_branching.utilities', level='ERROR'
            ):
                diff._update_conflicts()
        # Migrator returned None → comparison runs on the raw dicts.
        self.assertIsNone(diff.conflicts)
