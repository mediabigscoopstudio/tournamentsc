# Generated manually to add the basketball pre-match individual-scoring toggle.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0007_fixture_shot_clock_duration_seconds'),
    ]

    operations = [
        migrations.AddField(
            model_name='fixture',
            name='individual_scoring_enabled',
            field=models.BooleanField(default=True),
        ),
    ]
