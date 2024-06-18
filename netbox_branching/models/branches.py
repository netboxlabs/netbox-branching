import random
import string
from functools import cached_property, partial

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connection, models, transaction
from django.db.models.signals import post_save
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.utils.module_loading import import_string
from django.utils.translation import gettext_lazy as _

from core.models import Job, ObjectChange as ObjectChange_
from netbox.context_managers import event_tracking
from netbox.models import PrimaryModel
from netbox.models.features import JobsMixin
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import SCHEMA_PREFIX
from netbox_branching.contextvars import active_branch
from netbox_branching.signals import record_applied_change
from netbox_branching.utilities import activate_branch, get_branchable_object_types, get_tables_to_replicate
from utilities.exceptions import AbortRequest, AbortTransaction
from .changes import ObjectChange

__all__ = (
    'Branch',
)


class Branch(JobsMixin, PrimaryModel):
    name = models.CharField(
        verbose_name=_('name'),
        max_length=100,
        unique=True
    )
    user = models.ForeignKey(
        to=get_user_model(),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='branches'
    )
    schema_id = models.CharField(
        max_length=8,
        verbose_name=_('schema ID'),
        editable=False
    )
    status = models.CharField(
        verbose_name=_('status'),
        max_length=50,
        choices=BranchStatusChoices,
        default=BranchStatusChoices.NEW,
        editable=False
    )
    last_sync = models.DateTimeField(
        blank=True,
        null=True,
        editable=False
    )
    merged_time = models.DateTimeField(
        verbose_name=_('merged time'),
        blank=True,
        null=True
    )
    merged_by = models.ForeignKey(
        to=get_user_model(),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='+'
    )

    class Meta:
        ordering = ('name',)
        verbose_name = _('branch')
        verbose_name_plural = _('branches')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Generate a random schema ID if this is a new Branch
        if self.pk is None:
            self.schema_id = self._generate_schema_id()

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_branching:branch', args=[self.pk])

    def get_status_color(self):
        return BranchStatusChoices.colors.get(self.status)

    @cached_property
    def is_active(self):
        return self == active_branch.get()

    @property
    def ready(self):
        return self.status == BranchStatusChoices.READY

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

        if active_branch.get():
            raise AbortRequest(_("Cannot create or modify a branch while a branch is active."))

        super().save(*args, **kwargs)

        if _provision:
            # Enqueue a background job to provision the Branch
            Job.enqueue(
                import_string('netbox_branching.jobs.provision_branch'),
                instance=self,
                name='Provision branch'
            )

    def delete(self, *args, **kwargs):
        if active_branch.get():
            raise AbortRequest(_("Cannot delete a branch while a branch is active."))

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
        Return a queryset of all ObjectChange records created within the Branch.
        """
        if self.status == BranchStatusChoices.MERGED:
            return ObjectChange.objects.using(DEFAULT_DB_ALIAS).filter(
                application__branch=self
            )
        return ObjectChange.objects.using(self.connection_name)

    def get_unsynced_changes(self):
        """
        Return a queryset of all ObjectChange records created since the Branch
        was last synced or created.
        """
        return ObjectChange.objects.using(DEFAULT_DB_ALIAS).filter(
            changed_object_type__in=get_branchable_object_types(),
            time__gt=self.synced_time
        )

    def sync(self, commit=True):
        """
        Replay changes from the main schema onto the Branch's schema.
        """
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.SYNCING)

        with activate_branch(self):
            with transaction.atomic():
                for change in self.get_unsynced_changes().order_by('time'):
                    change.apply(using=self.connection_name)
                if not commit:
                    raise AbortTransaction()

        self.last_sync = timezone.now()
        self.status = BranchStatusChoices.READY
        self.save()

    sync.alters_data = True

    def merge(self, user, commit=True):
        """
        Apply all changes in the Branch to the main schema by replaying them in
        chronological order.
        """
        try:
            with transaction.atomic():
                # Retrieve staged changes before we update the Branch's status
                changes = self.get_changes().order_by('time')

                # Update the Branch's status to "merging"
                self.status = BranchStatusChoices.MERGING
                self.save()

                # Create a dummy request for the event_tracking() context manager
                request = RequestFactory().get(reverse('home'))

                # Prep & connect the signal receiver for recording AppliedChanges
                handler = partial(record_applied_change, branch=self)
                post_save.connect(handler, sender=ObjectChange_, weak=False)

                # Apply each change from the Branch
                for change in changes:
                    with event_tracking(request):
                        request.id = change.request_id
                        request.user = change.user
                        change.apply()
                if not commit:
                    raise AbortTransaction()

                # Update the Branch's status to "merged"
                self.status = BranchStatusChoices.MERGED
                self.merged_time = timezone.now()
                self.merged_by = user
                self.save()

                # TODO: Ensure handler is disconnected even when exception is raised
                # Disconnect the signal receiver
                post_save.disconnect(handler, sender=ObjectChange_)

        except ValidationError as e:
            messages = ', '.join(e.messages)
            raise ValidationError(f'{change.changed_object}: {messages}')

    merge.alters_data = True

    def provision(self):
        """
        Create the schema & replicate main tables.
        """
        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.PROVISIONING)

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

            # Replicate relevant tables from the main schema
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

        Branch.objects.filter(pk=self.pk).update(status=BranchStatusChoices.READY)

    provision.alters_data = True

    def deprovision(self):
        """
        Delete the Branch's schema and all its tables from the database.
        """
        with connection.cursor() as cursor:
            # Delete the schema and all its tables
            cursor.execute(
                f"DROP SCHEMA IF EXISTS {self.schema_name} CASCADE"
            )

    deprovision.alters_data = True
