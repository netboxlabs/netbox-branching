import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0002_branch_schema_id_unique'),
    ]

    operations = [
        migrations.AddField(
            model_name='branchevent',
            name='related_branch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='netbox_branching.branch'),  # noqa: E501
        ),
    ]
