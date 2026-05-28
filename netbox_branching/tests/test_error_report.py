"""
Tests for `netbox_branching.error_report`.

This module classifies exceptions raised during merge/revert into structured
report entries that surface in the job log. It is pure parsing/string-shaping
logic with no DB access, so the tests run as SimpleTestCase.
"""
from types import SimpleNamespace

from dcim.models import Site
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import SimpleTestCase

from netbox_branching.choices import BranchMergeStrategyChoices
from netbox_branching.constants import PG_UNIQUE_VIOLATION
from netbox_branching.error_report import (
    annotate_validation_error,
    build_error_report,
    get_entry_message,
    get_merge_recommendations,
)


class _FakePgCause(Exception):
    """
    Stand-in for the psycopg exception chained to a Django IntegrityError.
    Must derive from BaseException to be assignable to __cause__.
    """
    def __init__(self, sqlstate, table_name=None, constraint_name=None, message_detail=None):
        super().__init__()
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(
            table_name=table_name,
            constraint_name=constraint_name,
            message_detail=message_detail,
        )


def _make_integrity_error(**cause_kwargs):
    exc = IntegrityError('duplicate key value violates unique constraint')
    exc.__cause__ = _FakePgCause(**cause_kwargs)
    return exc


class BuildErrorReportTestCase(SimpleTestCase):
    """build_error_report dispatches on exception type and extracts structure."""

    def test_unique_violation_with_detail_extracts_field_and_value(self):
        exc = _make_integrity_error(
            sqlstate=PG_UNIQUE_VIOLATION,
            table_name='dcim_site',
            constraint_name='dcim_site_slug_key',
            message_detail='Key (slug)=(my-site) already exists.',
        )
        entry = build_error_report(exc)
        self.assertEqual(entry['type'], 'unique_constraint')
        self.assertEqual(entry['field'], 'slug')
        self.assertEqual(entry['value'], 'my-site')

    def test_unique_violation_without_detail_falls_back_gracefully(self):
        """No message_detail means field/value remain None, but type still classified."""
        exc = _make_integrity_error(
            sqlstate=PG_UNIQUE_VIOLATION,
            table_name='dcim_site',
            constraint_name=None,
            message_detail=None,
        )
        entry = build_error_report(exc)
        self.assertEqual(entry['type'], 'unique_constraint')
        self.assertIsNone(entry['field'])
        self.assertIsNone(entry['value'])

    def test_non_unique_integrity_error_classified_as_database_error(self):
        """A non-23505 sqlstate (e.g. FK violation) falls through to the generic branch."""
        exc = _make_integrity_error(sqlstate='23503')  # foreign_key_violation
        entry = build_error_report(exc)
        self.assertEqual(entry['type'], 'database_error')

    def test_validation_error_with_unique_code_classified_as_unique_constraint(self):
        """ValidationError carrying a 'unique' error code should map to unique_constraint."""
        exc = ValidationError({'slug': [ValidationError('duplicate', code='unique')]})
        entry = build_error_report(exc)
        self.assertEqual(entry['type'], 'unique_constraint')
        self.assertEqual(entry['field'], 'slug')

    def test_validation_error_without_unique_code_classified_as_validation_error(self):
        exc = ValidationError({'name': [ValidationError('too short', code='min_length')]})
        entry = build_error_report(exc)
        self.assertEqual(entry['type'], 'validation_error')
        self.assertEqual(entry['field'], 'name')

    def test_unknown_exception_classified_as_database_error(self):
        entry = build_error_report(RuntimeError('something else'))
        self.assertEqual(entry['type'], 'database_error')
        self.assertIsNone(entry['field'])

    def test_annotate_validation_error_attaches_branch_context(self):
        """annotate_validation_error decorates the exception for downstream lookup."""
        exc = ValidationError('boom')
        annotate_validation_error(exc, Site, object_id=7, content_type_id=42)
        entry = build_error_report(exc)
        self.assertEqual(entry['object_id'], 7)
        self.assertEqual(entry['content_type_id'], 42)
        self.assertEqual(entry['model'], 'site')


class GetEntryMessageTestCase(SimpleTestCase):

    def test_unique_constraint_with_full_context_includes_model_field_value(self):
        msg = get_entry_message({
            'type': 'unique_constraint',
            'model': 'site',
            'field': 'slug',
            'value': 'my-site',
        })
        # Don't pin the exact translated string — verify it carries the salient parts
        self.assertIn('Site', msg)
        self.assertIn('slug', msg)
        self.assertIn('my-site', msg)

    def test_unique_constraint_with_no_context_returns_generic_message(self):
        msg = get_entry_message({'type': 'unique_constraint'})
        self.assertIn('already exists', msg)

    def test_validation_error_includes_field_when_present(self):
        msg = get_entry_message({'type': 'validation_error', 'model': 'site', 'field': 'name'})
        self.assertIn('Site', msg)
        self.assertIn('name', msg)

    def test_database_error_returns_generic_message(self):
        msg = get_entry_message({'type': 'database_error'})
        self.assertIn('database error', msg.lower())


class GetMergeRecommendationsTestCase(SimpleTestCase):
    """
    Recommendations differ based on (entry type, merge strategy). The
    "switch to squash" suggestion must never appear when the user is
    already using squash — that would be obviously useless guidance.
    """

    def test_unique_constraint_iterative_suggests_rename_and_squash(self):
        recs = get_merge_recommendations(
            {'type': 'unique_constraint', 'field': 'slug', 'value': 'my-site'},
            merge_strategy=BranchMergeStrategyChoices.ITERATIVE,
        )
        # gettext_lazy returns __proxy__ objects — coerce to str before joining
        joined = ' '.join(str(r) for r in recs)
        self.assertEqual(len(recs), 2)
        self.assertIn('slug', joined)
        self.assertIn('Squash', joined)

    def test_unique_constraint_squash_omits_redundant_squash_suggestion(self):
        recs = get_merge_recommendations(
            {'type': 'unique_constraint', 'field': 'slug', 'value': 'my-site'},
            merge_strategy=BranchMergeStrategyChoices.SQUASH,
        )
        self.assertEqual(len(recs), 1)
        self.assertNotIn('Squash', str(recs[0]))

    def test_unique_constraint_without_field_uses_generic_rename(self):
        recs = get_merge_recommendations(
            {'type': 'unique_constraint'},
            merge_strategy=BranchMergeStrategyChoices.ITERATIVE,
        )
        # First rec is the generic rename guidance (no field/value interpolation)
        self.assertIn('Rename', str(recs[0]))

    def test_validation_error_with_field_recommends_fixing_that_field(self):
        recs = get_merge_recommendations(
            {'type': 'validation_error', 'field': 'name'},
            merge_strategy=BranchMergeStrategyChoices.ITERATIVE,
        )
        self.assertEqual(len(recs), 1)
        self.assertIn('name', str(recs[0]))

    def test_database_error_iterative_suggests_log_review_and_squash(self):
        recs = get_merge_recommendations(
            {'type': 'database_error'},
            merge_strategy=BranchMergeStrategyChoices.ITERATIVE,
        )
        self.assertEqual(len(recs), 2)

    def test_database_error_squash_only_suggests_log_review(self):
        recs = get_merge_recommendations(
            {'type': 'database_error'},
            merge_strategy=BranchMergeStrategyChoices.SQUASH,
        )
        self.assertEqual(len(recs), 1)
