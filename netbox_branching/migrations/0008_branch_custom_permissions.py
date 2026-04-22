from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_branching', '0007_branch_merge_strategy'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='branch',
            options={
                'ordering': ('name',),
                'permissions': [
                    ('sync', 'Synchronize branch with main schema'),
                    ('merge', 'Merge branch changes into main'),
                    ('migrate', 'Apply pending migrations to branch'),
                    ('revert', 'Revert a merged branch'),
                    ('archive', 'Archive a merged branch'),
                ],
            },
        ),
    ]
