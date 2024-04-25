from collections import defaultdict
from functools import cached_property

from django.contrib.auth import get_user_model
from django.db import connection, models
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from extras.choices import ObjectChangeActionChoices
from extras.models import ObjectChange
from netbox.context import current_request
from netbox.models import ChangeLoggedModel
from utilities.data import shallow_compare_dict
from utilities.serialization import serialize_object

from .todo import get_tables_to_replicate
from .utilities import get_active_context

__all__ = (
    'Context',
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
                'added': {},
                'removed': {},
            }

        entries = defaultdict(get_default)

        for change in ObjectChange.objects.order_by('time'):
            # Retrieve the object in its current form (outside the Context)
            model = change.changed_object_type.model_class()
            try:
                # TODO: Optimize object retrieval
                original = model.objects.using('default').get(pk=change.changed_object_id)
                prechange_data = serialize_object(original, exclude=['last_updated'])
            except model.DoesNotExist:
                print(f'did not find {change.changed_object_type} {change.changed_object_id}')
                prechange_data = {}

            diff_added = shallow_compare_dict(
                prechange_data,
                change.postchange_data or dict(),
                exclude=['last_updated'],
            )
            diff_removed = shallow_compare_dict(
                change.postchange_data or dict(),
                prechange_data,
                exclude=['last_updated'],
            )

            key = change.changed_object or original
            entries[key]['added'].update(diff_added)
            entries[key]['removed'].update(diff_removed)

        return dict(entries)

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
