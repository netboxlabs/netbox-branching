import logging
from functools import cached_property

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.postgres.fields import ArrayField
from django.db import DEFAULT_DB_ALIAS, models
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange as ObjectChange_
from netbox_branching.utilities import update_object
from utilities.querysets import RestrictedQuerySet
from utilities.serialization import deserialize_object

__all__ = (
    'AppliedChange',
    'ChangeDiff',
    'ObjectChange',
)


class ObjectChange(ObjectChange_):
    """
    Proxy model for NetBox's ObjectChange.
    """
    class Meta:
        proxy = True

    def migrate(self, branch, revert=False):
        """
        Run all applicable change data migrators for the given Branch, in sequence. This ensures that changes made prior
        to the application of a database migration can be successfully applied to (or reverted from) the main schema.
        """
        object_type = '.'.join(self.changed_object_type.natural_key())
        for migrator in branch.migrators.get(object_type, []):
            migrator(self, revert)

    def apply(self, branch, using=DEFAULT_DB_ALIAS, logger=None):
        """
        Apply the change using the specified database connection.
        """
        logger = logger or logging.getLogger('netbox_branching.models.ObjectChange.apply')
        model = self.changed_object_type.model_class()
        logger.info(f'Applying change {self.pk} using {using} ({self})')

        # Run data migrators
        self.migrate(branch)

        # Creating a new object
        if self.action == ObjectChangeActionChoices.ACTION_CREATE:
            if hasattr(model, 'deserialize_object'):
                instance = model.deserialize_object(self.postchange_data, pk=self.changed_object_id)
            else:
                instance = deserialize_object(model, self.postchange_data, pk=self.changed_object_id)
            try:
                instance.object.full_clean()
            except (FileNotFoundError) as e:
                # If a file was deleted later in this branch it will fail here
                # so we need to ignore it. We can assume the NetBox state is valid.
                logger.warning(f'Ignoring missing file: {e}')
            instance.save(using=using)

        # Modifying an object
        elif self.action == ObjectChangeActionChoices.ACTION_UPDATE:
            instance = model.objects.using(using).get(pk=self.changed_object_id)
            logger.debug(f'Updating {model._meta.verbose_name} {instance}')
            update_object(instance, self.diff()['post'], using=using)

        # Deleting an object
        elif self.action == ObjectChangeActionChoices.ACTION_DELETE:
            try:
                instance = model.objects.get(pk=self.changed_object_id)
                logger.debug(f'Deleting {model._meta.verbose_name} {instance}')
                instance.delete(using=using)
            except model.DoesNotExist:
                logger.debug(f'{model._meta.verbose_name} ID {self.changed_object_id} already deleted; skipping')

    apply.alters_data = True

    def undo(self, branch, using=DEFAULT_DB_ALIAS, logger=None):
        """
        Revert a previously applied change using the specified database connection.
        """
        logger = logger or logging.getLogger('netbox_branching.models.ObjectChange.undo')
        model = self.changed_object_type.model_class()
        logger.info(f'Undoing change {self.pk} using {using} ({self})')

        # Run data migrators
        self.migrate(branch, revert=True)

        # Deleting a previously created object
        if self.action == ObjectChangeActionChoices.ACTION_CREATE:
            try:
                instance = model.objects.get(pk=self.changed_object_id)
                logger.debug(f'Undoing creation of {model._meta.verbose_name} {instance}')
                instance.delete(using=using)
            except model.DoesNotExist:
                logger.debug(f'{model._meta.verbose_name} ID {self.changed_object_id} does not exist; skipping')

        # Reverting a modification to an object
        elif self.action == ObjectChangeActionChoices.ACTION_UPDATE:
            instance = model.objects.using(using).get(pk=self.changed_object_id)
            update_object(instance, self.diff()['pre'], using=using)

        # Restoring a deleted object
        elif self.action == ObjectChangeActionChoices.ACTION_DELETE:
            deserialized = deserialize_object(model, self.prechange_data_clean, pk=self.changed_object_id)
            instance = deserialized.object
            logger.debug(f'Restoring {model._meta.verbose_name} {instance}')

            # Restore GenericForeignKey fields
            for field in instance._meta.private_fields:
                if isinstance(field, GenericForeignKey):
                    ct_field = getattr(instance, field.ct_field)
                    fk_field = getattr(instance, field.fk_field)
                    if ct_field and fk_field:
                        setattr(instance, field.name, ct_field.get_object_for_this_type(pk=fk_field))

            instance.full_clean()
            instance.save(using=using)

    undo.alters_data = True


class ChangeDiff(models.Model):
    branch = models.ForeignKey(
        to='netbox_branching.Branch',
        on_delete=models.CASCADE
    )
    last_updated = models.DateTimeField(
        auto_now_add=True
    )
    object_type = models.ForeignKey(
        to='contenttypes.ContentType',
        on_delete=models.PROTECT,
        related_name='+'
    )
    object_id = models.PositiveBigIntegerField()
    object = GenericForeignKey(
        ct_field='object_type',
        fk_field='object_id'
    )
    object_repr = models.CharField(
        max_length=200,
        editable=False
    )
    action = models.CharField(
        verbose_name=_('action'),
        max_length=50,
        choices=ObjectChangeActionChoices
    )
    original = models.JSONField(
        blank=True,
        null=True
    )
    modified = models.JSONField(
        blank=True,
        null=True
    )
    current = models.JSONField(
        blank=True,
        null=True
    )
    conflicts = ArrayField(
        base_field=models.CharField(max_length=100),
        editable=False,
        blank=True,
        null=True
    )

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('-last_updated',)
        indexes = (
            models.Index(fields=('object_type', 'object_id')),
        )
        verbose_name = _('change diff')
        verbose_name_plural = _('change diffs')

    def __str__(self):
        return f'{self.get_action_display()} {self.object_type.name} {self.object_repr} ({self.object_id})'

    def save(self, *args, **kwargs):
        self._update_conflicts()
        if self.object:
            self.object_repr = str(self.object)

        super().save(*args, **kwargs)

    def get_action_color(self):
        return ObjectChangeActionChoices.colors.get(self.action)

    def _update_conflicts(self):
        """
        Record any conflicting changes between the modified and current object data.
        """
        if self.original is None or self.current is None:
            # Both the original and current states must be available to compare
            return
        conflicts = None
        if self.action == ObjectChangeActionChoices.ACTION_UPDATE:
            conflicts = [
                k for k, v in self.original.items()
                if v != self.modified[k] and v != self.current.get(k) and self.modified[k] != self.current.get(k)
            ]
        elif self.action == ObjectChangeActionChoices.ACTION_DELETE:
            conflicts = [
                k for k, v in self.original.items()
                if v != self.current.get(k)
            ]
        self.conflicts = conflicts or None

    @cached_property
    def altered_in_modified(self):
        """
        Return the set of attributes altered in the branch schema.
        """
        return {
            k for k, v in self.modified.items()
            if k in self.original and v != self.original[k]
        }

    @cached_property
    def altered_in_current(self):
        """
        Return the set of attributes altered in the main schema.
        """
        return {
            k for k, v in self.current.items()
            if k in self.original and v != self.original[k]
        }

    @cached_property
    def altered_fields(self):
        """
        Return an ordered list of attributes which have been modified in either the branch or main schema.
        """
        return sorted([*self.altered_in_modified, *self.altered_in_current])

    @cached_property
    def diff(self):
        """
        Provides a three-way summary of modified data, comparing the original, modified (branch), and current states.
        """
        return {
            'original': self.original_diff,
            'modified': self.modified_diff,
            'current': self.current_diff,
        }

    @cached_property
    def original_diff(self):
        """
        Return a key-value mapping of all attributes in the original state which have been modified.
        """
        return {
            k: v for k, v in self.original.items()
            if k in self.altered_fields
        }

    @cached_property
    def modified_diff(self):
        """
        Return a key-value mapping of all attributes which have been modified within the branch.
        """
        return {
            k: v for k, v in self.modified.items()
            if k in self.altered_fields
        }

    @cached_property
    def current_diff(self):
        """
        Return a key-value mapping of all attributes which have been modified outside the branch.
        """
        return {
            k: v for k, v in self.current.items()
            if k in self.altered_fields
        }


class AppliedChange(models.Model):
    """
    Maps an applied ObjectChange to a Branch.
    """
    change = models.OneToOneField(
        to='core.ObjectChange',
        on_delete=models.CASCADE,
        related_name='application'
    )
    branch = models.ForeignKey(
        to='netbox_branching.Branch',
        on_delete=models.CASCADE,
        related_name='applied_changes'
    )

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('branch', 'change')
        verbose_name = _('applied change')
        verbose_name_plural = _('applied changes')
