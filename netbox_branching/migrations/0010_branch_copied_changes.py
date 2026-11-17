from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0009_changediff_last_updated_auto_now'),
    ]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='copied_changes',
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]
