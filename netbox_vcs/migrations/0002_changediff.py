import django.contrib.postgres.fields
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('netbox_vcs', '0001_initial'),
    ]

    operations = [
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
                ('context', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='netbox_vcs.context')),
                ('object_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='contenttypes.contenttype')),
                ('conflicts', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=100), blank=True, editable=False, null=True, size=None))
            ],
            options={
                'verbose_name': 'change diff',
                'verbose_name_plural': 'change diffs',
                'ordering': ('-last_updated',),
                'indexes': [models.Index(fields=['object_type', 'object_id'], name='netbox_vcs__object__6c5231_idx')],
            },
        ),
    ]
