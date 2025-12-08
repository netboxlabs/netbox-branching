from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_branching", "0006_tag_object_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="branch",
            name="merge_strategy",
            field=models.CharField(blank=True, null=True, default=None, max_length=50),
        ),
    ]
