# Generated manually to add the organizer-selectable shot clock duration.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0006_tournament_registration_form_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='fixture',
            name='shot_clock_duration_seconds',
            field=models.PositiveSmallIntegerField(default=24),
        ),
    ]
