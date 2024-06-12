import random
import string
from functools import cached_property

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connection, models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.module_loading import import_string
from django.utils.translation import gettext_lazy as _
from mptt.models import MPTTModel

from core.models import Job
from extras.choices import ObjectChangeActionChoices
from extras.models import ObjectChange as ObjectChange_
from netbox.context import current_request
from netbox.models import NetBoxModel
from netbox.models.features import JobsMixin
from utilities.exceptions import AbortRequest, AbortTransaction
from utilities.querysets import RestrictedQuerySet
from utilities.serialization import deserialize_object
from .choices import ContextStatusChoices
from .constants import SCHEMA_PREFIX
from .contextvars import active_context
from .utilities import activate_context, get_context_aware_object_types, get_tables_to_replicate

__all__ = (
    'ChangeDiff',
    'Context',
    'ObjectChange',
)


class Context(JobsMixin, NetBoxModel):
    name = models.CharField(
        verbose_name=_('name'),
        max_length=100,
        unique=True
    )
    description = models.CharField(
        verbose_name=_('description'),
        max_length=200,
        blank=True
    )
    user = models.ForeignKey(
        to=get_user_model(),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='contexts'
    )
    schema_id = models.CharField(
        max_length=8,
        verbose_name=_('schema ID'),
        editable=False
    )
    status = models.CharField(
        verbose_name=_('status'),
        max_length=50,
        choices=ContextStatusChoices,
        default=ContextStatusChoices.NEW,
        editable=False
    )
    last_sync = models.DateTimeField(
        blank=True,
        null=True,
        editable=False
    )
    application_id = models.UUIDField(
        blank=True,
        null=True
    )

    class Meta:
        ordering = ('name',)
        verbose_name = _('context')
        verbose_name_plural = _('contexts')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Generate a random schema ID if this is a new Context
        if self.pk is None:
            self.schema_id = self._generate_schema_id()

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_vcs:context', args=[self.pk])

    def get_status_color(self):
        return ContextStatusChoices.colors.get(self.status)

    @cached_property
    def is_active(self):
        return self == active_context.get()

    @property
    def ready(self):
        return self.status == ContextStatusChoices.READY

    @cached_property
    def schema_name(self):
        return f'{SCHEMA_PREFIX}{self.schema_id}'

    @cached_property
    def connection_name(self):
        return f'schema_{self.schema_name}'

    @cached_property
    def synced_time(self):
        return self.last_sync or self.created

    def save(self, *args, **kwargs):
        _provision = self.pk is None

        if active_context.get():
            raise AbortRequest(_("Cannot create or modify a context while a context is active."))

        super().save(*args, **kwargs)

        if _provision:
            # Enqueue a background job to provision the Context
            Job.enqueue(
                import_string('netbox_vcs.jobs.provision_context'),
                instance=self,
                name='Provision context'
            )

    def delete(self, *args, **kwargs):
        if active_context.get():
            raise AbortRequest(_("Cannot delete a context while a context is active."))

        ret = super().delete(*args, **kwargs)

        self.deprovision()

        return ret

    @staticmethod
    def _generate_schema_id(length=8):
        """
        Generate a random alphanumeric schema identifier of the specified length.
        """
        chars = [*string.ascii_lowercase, *string.digits]
        return ''.join(random.choices(chars, k=length))

    def get_changes(self):
        """
        Return a queryset of all ObjectChange records created within the Context.
        """
        if not self.ready:
            return ObjectChange.objects.none()
        return ObjectChange.objects.using(self.connection_name)

    def get_unsynced_changes(self):
        """
        Return a queryset of all ObjectChange records created since the Context
        was last synced or created.
        """
        return ObjectChange.objects.using(DEFAULT_DB_ALIAS).filter(
            changed_object_type__in=get_context_aware_object_types(),
            time__gt=self.synced_time
        )

    def sync(self, commit=True):
        """
        Replay changes from the primary schema onto the Context's schema.
        """
        with activate_context(self):
            with transaction.atomic():
                Context.objects.filter(pk=self.pk).update(status=ContextStatusChoices.REBASING)
                for change in self.get_unsynced_changes().order_by('time'):
                    change.apply(using=self.connection_name)
                if not commit:
                    raise AbortTransaction()

        self.last_sync = timezone.now()
        self.status = ContextStatusChoices.READY
        self.save()

    def apply(self, commit=True):
        """
        Apply all changes in the Context to the primary schema by replaying them in
        chronological order.
        """
        try:
            with transaction.atomic():

                # Apply each change from the context
                for change in self.get_changes().order_by('time'):
                    change.apply()
                if not commit:
                    raise AbortTransaction()

                # Update the Context's status to "applied"
                self.status = ContextStatusChoices.APPLIED
                self.application_id = current_request.get().id
                self.save()

        except ValidationError as e:
            messages = ', '.join(e.messages)
            raise ValidationError(f'{change.changed_object}: {messages}')

    def provision(self):
        """
        Create the schema & replicate main tables.
        """
        Context.objects.filter(pk=self.pk).update(status=ContextStatusChoices.PROVISIONING)

        with connection.cursor() as cursor:
            schema = self.schema_name

            # Create the new schema
            cursor.execute(
                f"CREATE SCHEMA {schema}"
            )

            # Create an empty copy of the global change log. Share the ID sequence from the main table to avoid
            # reusing change record IDs.
            table = ObjectChange_._meta.db_table
            cursor.execute(
                f"CREATE TABLE {schema}.{table} ( LIKE public.{table} INCLUDING INDEXES )"
            )
            # Set the default value for the ID column to the sequence associated with the source table
            cursor.execute(
                f"ALTER TABLE {schema}.{table} "
                f"ALTER COLUMN id SET DEFAULT nextval('public.{table}_id_seq')"
            )

            # Replicate relevant tables from the primary schema
            for table in get_tables_to_replicate():
                # Create the table in the new schema
                cursor.execute(
                    f"CREATE TABLE {schema}.{table} ( LIKE public.{table} INCLUDING INDEXES )"
                )
                # Copy data from the source table
                cursor.execute(
                    f"INSERT INTO {schema}.{table} SELECT * FROM public.{table}"
                )
                # Set the default value for the ID column to the sequence associated with the source table
                cursor.execute(
                    f"ALTER TABLE {schema}.{table} ALTER COLUMN id SET DEFAULT nextval('public.{table}_id_seq')"
                )

        Context.objects.filter(pk=self.pk).update(status=ContextStatusChoices.READY)

    def deprovision(self):
        """
        Delete the context's schema and all its tables from the database.
        """
        with connection.cursor() as cursor:
            # Delete the schema and all its tables
            cursor.execute(
                f"DROP SCHEMA {self.schema_name} CASCADE"
            )


class ObjectChange(ObjectChange_):
    """
    Proxy model for NetBox's ObjectChange.
    """
    class Meta:
        proxy = True

    def apply(self, using=DEFAULT_DB_ALIAS):
        """
        Apply the change to the primary schema.
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
            instance.object.full_clean()
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
    context = models.ForeignKey(
        to=Context,
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
        Return the set of attributes altered in the context schema.
        """
        return {
            k for k, v in self.modified.items()
            if k in self.original and v != self.original[k]
        }

    @cached_property
    def altered_in_current(self):
        """
        Return the set of attributes altered in the primary schema.
        """
        return {
            k for k, v in self.current.items()
            if k in self.original and v != self.original[k]
        }

    @cached_property
    def altered_fields(self):
        """
        Return an ordered list of attributes which have been modified in either the context or primary schema.
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
        Return a key-value mapping of all attributes which have been modified within the context.
        """
        return {
            k: v for k, v in self.modified.items()
            if k in self.altered_fields
        }

    @cached_property
    def current_diff(self):
        """
        Return a key-value mapping of all attributes which have been modified outside the context.
        """
        return {
            k: v for k, v in self.current.items()
            if k in self.altered_fields
        }
