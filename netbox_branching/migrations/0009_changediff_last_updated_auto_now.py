from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0008_branch_custom_permissions'),
    ]

    operations = [
        migrations.AlterField(
            model_name='changediff',
            name='last_updated',
            field=models.DateTimeField(auto_now=True),
        ),
    ]
