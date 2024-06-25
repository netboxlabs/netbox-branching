from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0002_branch_rename_user_to_owner'),
    ]

    operations = [
        migrations.AddField(
            model_name='changediff',
            name='object_repr',
            field=models.CharField(default='', editable=False, max_length=200),
            preserve_default=False,
        ),
    ]
