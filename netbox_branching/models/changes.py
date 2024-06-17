from functools import cached_property

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.postgres.fields import ArrayField
from django.db import DEFAULT_DB_ALIAS, models
from django.utils.translation import gettext_lazy as _
from mptt.models import MPTTModel

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange as ObjectChange_
from utilities.querysets import RestrictedQuerySet
from utilities.serialization import deserialize_object

__all__ = (
    'ChangeDiff',
    'ObjectChange',
)


class ObjectChange(ObjectChange_):
    """
    Proxy model for NetBox's ObjectChange.
    """
    class Meta:
        proxy = True

    def apply(self, using=DEFAULT_DB_ALIAS):
        """
        Apply the change using the specified database connection.
        """
        model = self.changed_object_type.model_class()
        print(f'Applying change {self} using {using}')

        # Creating a new object
        if self.action == ObjectChangeActionChoices.ACTION_CREATE:
            instance = deserialize_object(model, self.postchange_data, pk=self.changed_object_id)
            print(f'Creating {model._meta.verbose_name} {instance}')
            instance.object.full_clean()
            instance.save(using=using)

        # Modifying an object
        elif self.action == ObjectChangeActionChoices.ACTION_UPDATE:
            instance = model.objects.using(using).get(pk=self.changed_object_id)
            for k, v in self.diff()['post'].items():
                # Assign FKs by integer
                # TODO: Inspect model to determine proper way to assign value
                if hasattr(instance, f'{k}_id'):
                    setattr(instance, f'{k}_id', v)
                else:
                    setattr(instance, k, v)
            print(f'Updating {model._meta.verbose_name} {instance}')
            instance.full_clean()
            instance.save(using=using)

        # Deleting an object
        elif self.action == ObjectChangeActionChoices.ACTION_DELETE:
            try:
                instance = model.objects.get(pk=self.changed_object_id)
                print(f'Deleting {model._meta.verbose_name} {instance}')
                instance.delete(using=using)
            except model.DoesNotExist:
                print(f'{model._meta.verbose_name} ID {self.changed_object_id} already deleted; skipping')

        # Rebuild the MPTT tree where applicable
        if issubclass(model, MPTTModel):
            model.objects.rebuild()

    apply.alters_data = True


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
        return f'{self.get_action_display()} {self.object_type.name} {self.object_id}'

    def save(self, *args, **kwargs):
        self._update_conflicts()

        super().save(*args, **kwargs)

    def get_action_color(self):
        return ObjectChangeActionChoices.colors.get(self.action)

    def _update_conflicts(self):
        """
        Record any conflicting changes between the modified and current object data.
        """
        conflicts = None
        if self.action == ObjectChangeActionChoices.ACTION_UPDATE:
            conflicts = [
                k for k, v in self.original.items()
                if v != self.modified[k] and v != self.current[k] and self.modified[k] != self.current[k]
            ]
        elif self.action == ObjectChangeActionChoices.ACTION_DELETE:
            conflicts = [
                k for k, v in self.original.items()
                if v != self.current[k]
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
