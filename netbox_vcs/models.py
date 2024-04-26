from collections import defaultdict
from functools import cached_property

from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connection, models, transaction
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from extras.choices import ObjectChangeActionChoices
from extras.models import ObjectChange as ObjectChange_
from netbox.models import ChangeLoggedModel
from utilities.data import shallow_compare_dict
from utilities.exceptions import AbortTransaction
from utilities.serialization import deserialize_object, serialize_object

from .constants import DIFF_EXCLUDE_FIELDS
from .todo import get_tables_to_replicate
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
    schema_name = models.CharField(
        max_length=63,  # PostgreSQL limit on schema name length
        verbose_name=_('schema name'),
        editable=False
    )

    class Meta:
        ordering = ('name',)
        verbose_name = _('context')
        verbose_name_plural = _('contexts')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_vcs:context', args=[self.pk])

    @cached_property
    def is_active(self):
        active_context = get_active_context()
        return self.schema_name == active_context

    def clean(self):
        # Generate the schema name from the Context name (if not already set)
        if not self.schema_name:
            self.schema_name = slugify(self.name)[:63]

        super().clean()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        self.provision()

    def delete(self, *args, **kwargs):
        ret = super().delete(*args, **kwargs)

        self.deprovision()

        return ret

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

        for change in ObjectChange.objects.using(f'schema_{self.schema_name}').order_by('time'):
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

    def apply(self, commit=True):
        with transaction.atomic():
            for change in ObjectChange.objects.using(f'schema_{self.schema_name}').order_by('time'):
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

    def apply(self):
        """
        Apply the change to the primary schema.
        """
        model = self.changed_object_type.model_class()

        # Creating a new object
        if self.action == ObjectChangeActionChoices.ACTION_CREATE:
            instance = deserialize_object(model, self.postchange_data, pk=self.changed_object_id)
            print(f'Creating {model._meta.verbose_name} {instance}')
            instance.save(using=DEFAULT_DB_ALIAS)

        # Modifying an object
        elif self.action == ObjectChangeActionChoices.ACTION_UPDATE:
            instance = model.objects.get(pk=self.changed_object_id)
            for k, v in self.diff()['post'].items():
                setattr(instance, k, v)
            print(f'Updating {model._meta.verbose_name} {instance}')
            instance.save(using=DEFAULT_DB_ALIAS)

        # Deleting an object
        elif self.action == ObjectChangeActionChoices.ACTION_DELETE:
            instance = model.objects.get(pk=self.changed_object_id)
            print(f'Deleting {model._meta.verbose_name} {instance}')
            instance.delete(using=DEFAULT_DB_ALIAS)
    apply.alters_data = True
