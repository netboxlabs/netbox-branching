from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='branch',
            name='schema_id',
            field=models.CharField(editable=False, max_length=8, unique=True),
        ),
    ]
