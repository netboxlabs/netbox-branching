import random
import string
from collections import defaultdict
from functools import cached_property

from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connection, models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from extras.choices import ObjectChangeActionChoices
from extras.models import ObjectChange as ObjectChange_
from netbox.models import ChangeLoggedModel
from utilities.data import shallow_compare_dict
from utilities.exceptions import AbortTransaction
from utilities.serialization import deserialize_object, serialize_object

from .constants import DIFF_EXCLUDE_FIELDS, SCHEMA_PREFIX
from .todo import get_relevant_content_types, get_tables_to_replicate
from .utilities import get_active_context

__all__ = (
    'Context',
    'ObjectChange',
)


class Context(ChangeLoggedModel):
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
    rebase_time = models.DateTimeField(
        blank=True,
        null=True,
        editable=False
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

    @cached_property
    def is_active(self):
        return self == get_active_context()

    @cached_property
    def schema_name(self):
        return f'{SCHEMA_PREFIX}{self.schema_id}'

    @cached_property
    def connection_name(self):
        return f'schema_{self.schema_name}'

    def save(self, *args, **kwargs):
        _provision = self.pk is None

        super().save(*args, **kwargs)

        if _provision:
            self.provision()

    def delete(self, *args, **kwargs):
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

    def diff(self):
        """
        Return a summary of changes made within this Context relative to the primary.
        """
        def get_default():
            return {
                'current': {},
                'changed': {
                    'pre': {},
                    'post': {},
                },
            }

        entries = defaultdict(get_default)

        for change in ObjectChange.objects.using(self.connection_name).order_by('time'):
            model = change.changed_object_type.model_class()

            # Retrieve the object in its current form (outside the Context)
            try:
                # TODO: Optimize object retrieval
                instance = model.objects.using('default').get(pk=change.changed_object_id)
                instance_serialized = serialize_object(instance, exclude=['last_updated'])
            except model.DoesNotExist:
                instance = change.changed_object
                instance_serialized = {}

            changed_in_context = change.diff()
            current_data = {
                k: v for k, v in sorted(instance_serialized.items())
                if k in changed_in_context['post']
            }

            key = f'{change.changed_object_type}:{change.changed_object_id}'
            entries[key]['object'] = instance
            entries[key]['object_repr'] = change.object_repr
            entries[key]['current'].update(current_data)
            entries[key]['changed']['post'].update(changed_in_context['post'])
            for k, v in changed_in_context['pre'].items():
                if k not in entries[key]['changed']['pre']:
                    entries[key]['changed']['pre'][k] = v

        return dict(entries)

    def rebase(self, commit=True):
        """
        Replay changes from main onto the Context's schema.
        """
        start_time = self.rebase_time or self.created
        changes = ObjectChange.objects.using(DEFAULT_DB_ALIAS).filter(
            changed_object_type__in=get_relevant_content_types(),
            time__gt=start_time
        ).order_by('time')

        with transaction.atomic():
            for change in changes:
                change.apply(using=self.connection_name)
            if not commit:
                raise AbortTransaction()

        self.rebase_time = timezone.now()
        self.save()

    def apply(self, commit=True):
        with transaction.atomic():
            for change in ObjectChange.objects.using(self.connection_name).order_by('time'):
                change.apply()
            if not commit:
                raise AbortTransaction()

    def provision(self):
        """
        Create the schema & replicate main tables.
        """
        with connection.cursor() as cursor:
            schema = self.schema_name

            # Create the new schema
            cursor.execute(
                f"CREATE SCHEMA {schema}"
            )

            # Create an empty copy of the global change log
            cursor.execute(
                f"CREATE TABLE {schema}.extras_objectchange ( LIKE public.extras_objectchange INCLUDING ALL )"
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

    def deprovision(self):
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

    def diff(self):
        """
        Return a dictionary of pre- and post-change values for attribute values which have changed.
        """
        prechange_data = self.prechange_data or {}
        postchange_data = self.postchange_data or {}

        if self.action == ObjectChangeActionChoices.ACTION_CREATE:
            changed_attrs = sorted(postchange_data.keys())
        elif self.action == ObjectChangeActionChoices.ACTION_DELETE:
            changed_attrs = sorted(prechange_data.keys())
        else:
            # TODO: Support deep (recursive) comparison
            changed_data = shallow_compare_dict(
                prechange_data,
                postchange_data,
                exclude=DIFF_EXCLUDE_FIELDS  # TODO: Omit all read-only fields
            )
            changed_attrs = sorted(changed_data.keys())

        return {
            'pre': {
                k: prechange_data.get(k) for k in changed_attrs
            },
            'post': {
                k: postchange_data.get(k) for k in changed_attrs
            },
        }

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
            instance.save(using=using)

        # Deleting an object
        elif self.action == ObjectChangeActionChoices.ACTION_DELETE:
            try:
                instance = model.objects.get(pk=self.changed_object_id)
                print(f'Deleting {model._meta.verbose_name} {instance}')
                instance.delete(using=using)
            except model.DoesNotExist:
                print(f'{model._meta.verbose_name} ID {self.changed_object_id} already deleted; skipping')
    apply.alters_data = True
