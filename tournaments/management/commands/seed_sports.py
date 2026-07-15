from django.core.management.base import BaseCommand

from tournaments.constants import SPORTS
from tournaments.models import Sport


class Command(BaseCommand):
    help = 'Seed the 7 fixed sports (idempotent). Run in every environment.'

    def handle(self, *args, **options):
        created = 0
        for slug, (name, icon, color, ftype, default_fmt, _allowed) in SPORTS.items():
            obj, was_created = Sport.objects.update_or_create(
                slug=slug,
                defaults={'name': name, 'icon': icon, 'color_token': color,
                          'format_type': ftype, 'default_format': default_fmt},
            )
            created += int(was_created)
        self.stdout.write(self.style.SUCCESS(
            f'Sports seeded. {Sport.objects.count()} total ({created} new).'))
