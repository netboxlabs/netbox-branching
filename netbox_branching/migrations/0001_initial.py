import django.contrib.postgres.fields
import django.db.models.deletion
import taggit.managers
from django.conf import settings
from django.db import migrations, models

import utilities.json


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('core', '0011_move_objectchange'),
        ('extras', '0116_move_objectchange'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ObjectChange',
            fields=[
            ],
            options={
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('core.objectchange',),
        ),
        migrations.CreateModel(
            name='Branch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('comments', models.TextField(blank=True)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('schema_id', models.CharField(editable=False, max_length=8)),
                ('status', models.CharField(default='new', editable=False, max_length=50)),
                ('last_sync', models.DateTimeField(blank=True, editable=False, null=True)),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='branches', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'branch',
                'verbose_name_plural': 'branches',
                'ordering': ('name',),
            },
        ),
        migrations.CreateModel(
            name='ChangeDiff',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('last_updated', models.DateTimeField(auto_now_add=True)),
                ('object_id', models.PositiveBigIntegerField()),
                ('action', models.CharField(max_length=50)),
                ('original', models.JSONField(blank=True, null=True)),
                ('modified', models.JSONField(blank=True, null=True)),
                ('current', models.JSONField(blank=True, null=True)),
                ('conflicts', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=100), blank=True, editable=False, null=True, size=None)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='netbox_branching.branch')),
                ('object_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='contenttypes.contenttype')),
            ],
            options={
                'verbose_name': 'change diff',
                'verbose_name_plural': 'change diffs',
                'ordering': ('-last_updated',),
                'indexes': [models.Index(fields=['object_type', 'object_id'], name='netbox_bran_object__462279_idx')],
            },
        ),
    ]
