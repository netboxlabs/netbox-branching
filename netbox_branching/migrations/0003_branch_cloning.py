import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0002_branch_schema_id_unique'),
    ]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='origin',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='clones', to='netbox_branching.branch'),
        ),
        migrations.AddField(
            model_name='branch',
            name='origin_ptr',
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
