import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0004_copy_migrations'),
    ]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='applied_migrations',
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=200),
                blank=True,
                default=list,
                size=None
            ),
        ),
    ]
