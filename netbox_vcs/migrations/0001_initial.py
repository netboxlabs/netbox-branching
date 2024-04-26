import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Context',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('schema_name', models.CharField(editable=False, max_length=63)),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='contexts', to=settings.AUTH_USER_MODEL)),
                ('rebase_time', models.DateTimeField(blank=True, null=True, editable=False)),
            ],
            options={
                'verbose_name': 'context',
                'verbose_name_plural': 'contexts',
                'ordering': ('name',),
            },
        ),
        migrations.CreateModel(
            name='ObjectChange',
            fields=[
            ],
            options={
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('extras.objectchange',),
        ),
    ]
