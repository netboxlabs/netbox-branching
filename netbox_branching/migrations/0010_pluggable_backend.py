from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0009_changediff_last_updated_auto_now'),
    ]

    operations = [
        migrations.RenameField(
            model_name='branch',
            old_name='schema_id',
            new_name='backend_id',
        ),
        migrations.AlterField(
            model_name='branch',
            name='backend_id',
            field=models.CharField(blank=True, editable=False, max_length=255, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='branch',
            name='connection_params',
            field=models.JSONField(blank=True, editable=False, null=True),
        ),
    ]
