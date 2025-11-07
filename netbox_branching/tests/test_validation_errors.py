from unittest.mock import Mock, patch
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.choices import ObjectChangeActionChoices
from netbox_branching.models import ObjectChange


class ObjectChangeValidationTestCase(TestCase):
    """
    Test cases for ObjectChange.apply() and undo() validation error handling.
    """

    def test_apply_create_handles_validation_error(self):
        """
        Test that apply() gracefully handles ValidationError when creating objects.
        Fixes issue #356: Blank field validation errors should not crash sync operations.
        """
        # Create a mock ObjectChange for CREATE action
        change = ObjectChange()
        change.pk = 1
        change.action = ObjectChangeActionChoices.ACTION_CREATE
        change.changed_object_id = 123
        change.postchange_data = {'id': 123, 'comments': ''}  # Blank comments will fail validation

        # Mock the model class to simulate validation failure
        mock_model = Mock()
        mock_model._meta.verbose_name = 'TestModel'

        # Mock deserialized instance that will raise ValidationError on full_clean()
        mock_instance = Mock()
        mock_instance.object.full_clean.side_effect = ValidationError({'comments': ['This field cannot be blank.']})

        # Mock the changed_object_type
        change.changed_object_type = Mock()
        change.changed_object_type.model_class.return_value = mock_model

        # Mock deserialize_object to return our mock instance
        with patch('netbox_branching.models.changes.deserialize_object', return_value=mock_instance):
            with patch('netbox_branching.models.changes.ObjectChange.migrate'):
                # This should not raise an exception
                try:
                    change.apply(branch=None)
                except ValidationError:
                    self.fail('apply() should not raise ValidationError')

        # Verify full_clean was called
        mock_instance.object.full_clean.assert_called_once()
        # Verify save was NOT called (because validation failed)
        mock_instance.save.assert_not_called()

    def test_apply_create_handles_file_not_found_error(self):
        """
        Test that apply() still handles FileNotFoundError (existing behavior).
        """
        change = ObjectChange()
        change.pk = 1
        change.action = ObjectChangeActionChoices.ACTION_CREATE
        change.changed_object_id = 123
        change.postchange_data = {'id': 123}

        mock_model = Mock()
        mock_model._meta.verbose_name = 'TestModel'

        mock_instance = Mock()
        mock_instance.object.full_clean.side_effect = FileNotFoundError('File missing')

        change.changed_object_type = Mock()
        change.changed_object_type.model_class.return_value = mock_model

        with patch('netbox_branching.models.changes.deserialize_object', return_value=mock_instance):
            with patch('netbox_branching.models.changes.ObjectChange.migrate'):
                # This should not raise an exception
                try:
                    change.apply(branch=None)
                except FileNotFoundError:
                    self.fail('apply() should not raise FileNotFoundError')

    def test_undo_delete_handles_validation_error(self):
        """
        Test that undo() gracefully handles ValidationError when restoring deleted objects.
        """
        change = ObjectChange()
        change.pk = 1
        change.action = ObjectChangeActionChoices.ACTION_DELETE
        change.changed_object_id = 123
        change.prechange_data_clean = {'id': 123, 'comments': ''}

        mock_model = Mock()
        mock_model._meta.verbose_name = 'TestModel'

        # Mock deserialized instance
        mock_deserialized = Mock()
        mock_instance = Mock()
        mock_instance._meta.private_fields = []  # No GenericForeignKey fields
        mock_instance.full_clean.side_effect = ValidationError({'comments': ['This field cannot be blank.']})
        mock_deserialized.object = mock_instance

        change.changed_object_type = Mock()
        change.changed_object_type.model_class.return_value = mock_model

        with patch('netbox_branching.models.changes.deserialize_object', return_value=mock_deserialized):
            with patch('netbox_branching.models.changes.ObjectChange.migrate'):
                # This should not raise an exception
                try:
                    change.undo(branch=None)
                except ValidationError:
                    self.fail('undo() should not raise ValidationError')

        # Verify full_clean was called
        mock_instance.full_clean.assert_called_once()
        # Verify save was NOT called (because validation failed)
        mock_instance.save.assert_not_called()
