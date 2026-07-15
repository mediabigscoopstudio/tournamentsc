"""Seed a realistic demo dataset spanning all four format engines.

Idempotent-ish: safe to run once on a fresh DB. Creates an admin, an approved
organizer, players, and one tournament per engine with fixtures + results.

    python manage.py seed_demo
"""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import OrganizerProfile, PlayerProfile, User
from tournaments import constants as C
from tournaments.models import (FixtureParticipant, IndividualRegistration, Sport, Team,
                                TeamMembership, Tournament, TournamentTeamEntry)
from tournaments.services import apply_result

ADMIN = ('admin@tournamentsc.app', 'admin12345')
ORG = ('organizer@tournamentsc.app', 'organizer12345')


class Command(BaseCommand):
    help = 'Seed demo data across all four engines.'

    def handle(self, *args, **opts):
        Sport.objects.exists() or self.stdout.write('Run seed_sports first.')
        admin = self._admin()
        org = self._organizer()
        self.stdout.write(self.style.SUCCESS(
            f'Admin: {ADMIN[0]} / {ADMIN[1]}   Organizer: {ORG[0]} / {ORG[1]}'))

        today = timezone.now().date()
        soon = today + datetime.timedelta(days=7)

        self._bracket(org, today, soon)
        self._round_robin(org, today, soon)
        self._marathon(org, today, soon)
        self._racing(org, today, soon)
        self._esports(org, today, soon)
        self._pickleball(org, today, soon)
        self._highlights(org)
        self._content(admin)
        self.stdout.write(self.style.SUCCESS('Demo data seeded.'))

    def _pickleball(self, org, start, end):
        t, created = self._new_tournament(
            org, 'Dink Masters League', 'pickleball', C.FORMAT_KNOCKOUT, start, end,
            prize=20000, venue=self._venue('Whitefield Courts'),
            rules='Singles knockout. Best of 3 games to 11, win by 2.')
        if not created:
            return
        for i, nm in enumerate(['A. Menon', 'B. Sethi', 'C. Dutta', 'D. Roy',
                                'E. Kapoor', 'F. Nanda', 'G. Bose', 'H. Iyer'], start=1):
            p = self._player(nm)
            IndividualRegistration.objects.create(tournament=t, player=p, display_name=nm, seed=i)
        t.engine.generate_fixtures()
        t.fixtures_generated = True
        t.save()
        # Score round 1 with two-digit scores — this is exactly the shape that used
        # to pick the wrong winner when scores were compared as strings ('9' > '11').
        for fx in t.fixtures.filter(round_no=1, status='SCHEDULED'):
            parts = list(fx.participants.all())
            if len(parts) == 2 and all(p.player for p in parts):
                apply_result(fx, {'finalize': True,
                                  str(parts[0].id): {'score': '11'},
                                  str(parts[1].id): {'score': '9'}})

    def _highlights(self, org):
        """A published highlight per tournament, with a runtime and some views."""
        from tournaments.models import Highlight
        if Highlight.objects.exists():
            return
        specs = [
            ('City Basketball Open', 'Overtime thriller in the quarter-finals', 192, 24000),
            ('GDG Chess Open', 'The queen sacrifice that decided round 4', 365, 18000),
            ('BGMI City Scrims', 'Team Phantom take the final circle', 340, 41000),
            ('Kalinga Half Marathon', 'The sprint finish for gold', 168, 9000),
        ]
        for name, title, seconds, views in specs:
            t = Tournament.objects.filter(name=name).first()
            if not t:
                continue
            Highlight.objects.create(
                tournament=t, fixture=t.fixtures.filter(status='COMPLETED').first(),
                title=title, description='Match recap.',
                best_performer='—', duration_seconds=seconds, view_count=views,
                published_at=timezone.now(), created_by=org.user)

    # -- admin-managed platform content --------------------------------
    def _content(self, admin):
        """News, an announcement and platform settings — the resources the admin
        console owns, so the dashboard isn't empty on a fresh install."""
        from dash.models import Announcement, News, SiteSetting

        SiteSetting.load()  # materialise the singleton settings row

        if not News.objects.exists():
            first = Tournament.objects.filter(is_featured=True).first()
            News.objects.create(
                title='The season is open across all seven sports',
                summary='Brackets, points tables, time trials and mass-start events — '
                        'every format is live on TournamentSC.',
                body=('Local organizers can now run a tournament in any of the four format '
                      'engines, and audiences can follow every score without an account.\n\n'
                      'Head to Browse to find an event near you.'),
                sport=first.sport if first else None,
                tournament=first,
                status=News.STATUS_PUBLISHED, published_at=timezone.now(),
                created_by=admin)

        if not Announcement.objects.exists():
            Announcement.objects.create(
                title='Finals weekend',
                message='Championship matches across every sport go live this Saturday.',
                level='info', status=Announcement.STATUS_PUBLISHED,
                published_at=timezone.now(), created_by=admin)

    # -- accounts ------------------------------------------------------
    def _admin(self):
        u = User.objects.filter(email=ADMIN[0]).first()
        if not u:
            u = User.objects.create_superuser(email=ADMIN[0], password=ADMIN[1], first_name='Platform Admin')
        return u

    def _organizer(self):
        u = User.objects.filter(email=ORG[0]).first()
        if not u:
            u = User.objects.create_user(email=ORG[0], password=ORG[1], first_name='Demo Organizer')
        prof, _ = OrganizerProfile.objects.get_or_create(user=u)
        prof.is_approved = True
        prof.organization_name = 'Bhubaneswar Sports Collective'
        prof.approved_at = timezone.now()
        prof.save()
        return prof

    def _player(self, name, city='Bhubaneswar'):
        email = name.lower().replace(' ', '.') + '@example.com'
        u = User.objects.filter(email=email).first()
        if not u:
            u = User.objects.create_user(email=email, password='player12345', first_name=name)
        prof, _ = PlayerProfile.objects.get_or_create(user=u, defaults={'city': city})
        return prof

    def _new_tournament(self, org, name, sport_slug, fmt, start, end, featured=False,
                        prize=None, rules='', venue=None):
        sport = Sport.objects.get(slug=sport_slug)
        t, created = Tournament.objects.get_or_create(
            name=name, organizer=org,
            defaults=dict(sport=sport, format=fmt, city='Bhubaneswar', venue=venue,
                          start_date=start, end_date=end, status='PUBLISHED',
                          description=f'A demo {sport.name} event showcasing the {fmt} engine.',
                          rules=rules, prize_pool=prize,
                          is_featured=featured, featured_order=1 if featured else 0))

        # Backfill metadata onto a row seeded before these fields existed, so
        # re-running the seeder on an older database brings it up to date rather
        # than silently leaving the new columns empty.
        if not created:
            changed = []
            if prize is not None and t.prize_pool is None:
                t.prize_pool = prize
                changed.append('prize_pool')
            if rules and not t.rules:
                t.rules = rules
                changed.append('rules')
            if venue is not None and t.venue_id is None:
                t.venue = venue
                changed.append('venue')
            if changed:
                t.save(update_fields=changed + ['updated_at'])
        return t, created

    def _venue(self, name, city='Bhubaneswar'):
        from tournaments.models import Venue
        v, _ = Venue.objects.get_or_create(name=name, city=city)
        return v

    # -- one tournament per engine ------------------------------------
    def _bracket(self, org, start, end):
        t, created = self._new_tournament(
            org, 'City Basketball Open', 'basketball', C.FORMAT_KNOCKOUT, start, end,
            featured=True, prize=40000,
            venue=self._venue('Koramangala Indoor Arena'),
            rules=('Eight teams, single-elimination. Games are 4 quarters. '
                   'A tie is settled by a short overtime, then free throws.'))
        if not created:
            return
        names = ['Ravens Club', 'Titans BBC', 'Coastal Kings', 'Northside Hawks',
                 'Delta Ballers', 'Iron Five', 'Sunrisers', 'Metro Giants']
        for i, nm in enumerate(names, 1):
            team = Team.objects.create(name=nm, sport=t.sport)
            TournamentTeamEntry.objects.create(tournament=t, team=team, seed=i)
        t.engine.generate_fixtures()
        t.fixtures_generated = True
        t.save()
        # Score round 1
        for fx in t.fixtures.filter(round_no=1, status='SCHEDULED'):
            parts = list(fx.participants.all())
            if len(parts) == 2 and parts[0].team and parts[1].team:
                data = {'finalize': True,
                        str(parts[0].id): {'score': 78}, str(parts[1].id): {'score': 65}}
                apply_result(fx, data)

    RATINGS = {'Arjun Mehta': 2412, 'Priya Rao': 2388,
               'Sam Fernandes': 2301, 'Neha Das': 2356}

    def _round_robin(self, org, start, end):
        t, created = self._new_tournament(
            org, 'GDG Chess Open', 'chess', C.FORMAT_ROUND_ROBIN, start, end,
            prize=15000, venue=self._venue('Indiranagar Chess Club'),
            rules=('Round robin, 3 + 2 blitz. Standings are decided on points, then '
                   'Buchholz — the sum of your opponents\' scores.'))

        # Ratings drive the Rating column and give Buchholz something to separate.
        for nm, rating in self.RATINGS.items():
            p = self._player(nm)
            if p.rating != rating:
                p.rating = rating
                p.save(update_fields=['rating'])

        if not created:
            # Older database: push the ratings onto existing participants and
            # recompute so the Rating / Buchholz columns populate.
            for fp in FixtureParticipant.objects.filter(
                    fixture__tournament=t, player__isnull=False).select_related('player'):
                if fp.player.rating and (fp.stats or {}).get('rating') != fp.player.rating:
                    stats = dict(fp.stats or {})
                    stats['rating'] = fp.player.rating
                    fp.stats = stats
                    fp.save(update_fields=['stats'])
            t.engine.compute_standings()
            return

        for nm in self.RATINGS:
            IndividualRegistration.objects.create(
                tournament=t, player=self._player(nm), display_name=nm)
        t.engine.generate_fixtures()
        t.fixtures_generated = True
        t.save()
        fixtures = list(t.fixtures.filter(status='SCHEDULED'))
        for i, fx in enumerate(fixtures):
            parts = list(fx.participants.all())
            a, b = (1, 0) if i % 2 else (1, 1)  # some wins, some draws
            data = {'finalize': True,
                    str(parts[0].id): {'score': a}, str(parts[1].id): {'score': b}}
            apply_result(fx, data)

    def _marathon(self, org, start, end):
        from tournaments.models import EventCategory
        t, created = self._new_tournament(
            org, 'Kalinga Half Marathon', 'marathon', C.FORMAT_SINGLE_EVENT, start, end,
            prize=25000, venue=self._venue('Kalinga Stadium'),
            rules='Chip timing from start mat to finish mat. Results are official once the '
                  'category closes.')

        # A distance on the category is what turns on the pace column.
        cat, _ = EventCategory.objects.get_or_create(
            tournament=t, name='Half Marathon', defaults={'distance_km': 21.10})
        if cat.distance_km is None:
            cat.distance_km = 21.10
            cat.save(update_fields=['distance_km'])

        if not created:
            # Older database: attach the category to the race and denormalise the
            # bib onto each participant so Bib/Pace populate.
            t.fixtures.filter(event_category__isnull=True).update(event_category=cat)
            t.registrations.filter(event_category__isnull=True).update(event_category=cat)
            for fp in FixtureParticipant.objects.filter(
                    fixture__tournament=t, player__isnull=False).select_related('player'):
                reg = t.registrations.filter(player=fp.player).first()
                if reg and reg.bib_number and (fp.stats or {}).get('bib') != reg.bib_number:
                    stats = dict(fp.stats or {})
                    stats['bib'] = reg.bib_number
                    fp.stats = stats
                    fp.save(update_fields=['stats'])
            return

        runners = ['Ravi Kumar', 'Anita Sahoo', 'John Pinto', 'Meera Nair', 'Tom Alva', 'Zoya Khan']
        for i, nm in enumerate(runners):
            p = self._player(nm)
            IndividualRegistration.objects.create(tournament=t, player=p, display_name=nm,
                                                  event_category=cat, bib_number=str(1001 + i))
        t.engine.generate_fixtures()
        t.fixtures_generated = True
        t.save()
        fx = t.fixtures.first()
        base = 90 * 60 * 1000  # 1h30m in ms
        data = {'finalize': True}
        for i, p in enumerate(fx.participants.all()):
            data[str(p.id)] = {'time_ms': base + i * 45000, 'result_state': 'OK'}
        apply_result(fx, data)

    def _racing(self, org, start, end):
        t, created = self._new_tournament(org, 'Coastal Karting GP', 'racing',
                                          C.FORMAT_TIME_TRIAL, start, end)
        if not created:
            return
        for i, nm in enumerate(['P. Sharma', 'K. Reddy', 'L. Dsouza', 'M. Iqbal', 'R. Bose']):
            p = self._player(nm)
            IndividualRegistration.objects.create(tournament=t, player=p, display_name=nm)
        t.engine.generate_fixtures()
        t.fixtures_generated = True
        t.save()
        fx = t.fixtures.filter(round_name='Race').first() or t.fixtures.first()
        base = 84000  # ~1:24 laps
        data = {'finalize': True}
        for i, p in enumerate(fx.participants.all()):
            data[str(p.id)] = {'time_ms': base + i * 300, 'result_state': 'OK'}
        apply_result(fx, data)

    def _esports(self, org, start, end):
        t, created = self._new_tournament(
            org, 'BGMI City Scrims', 'mobile-esports', C.FORMAT_ROUND_ROBIN, start, end,
            prize=100000,
            rules='Six matches. Each squad scores placement points plus one point per kill; '
                  'the leaderboard totals across every match.')
        if not created:
            # Older database: recompute so the placement/kills breakdown lands in
            # standings.extra_stats for the public leaderboard columns.
            t.engine.compute_standings()
            return
        squads = ['Team Phantom', 'Nova Esports', 'Ghost Squad', 'Apex Wolves', 'Void Gaming', 'Titan X']
        for nm in squads:
            team = Team.objects.create(name=nm, sport=t.sport)
            TournamentTeamEntry.objects.create(tournament=t, team=team)
        t.engine.generate_fixtures()
        t.fixtures_generated = True
        t.save()
        for m, fx in enumerate(t.fixtures.all()):
            data = {'finalize': True}
            for i, p in enumerate(fx.participants.all()):
                data[str(p.id)] = {'kills': (i + m) % 12, 'placement': i + 1}
            apply_result(fx, data)
        # Leave the last match LIVE for the demo
        last = t.fixtures.last()
        if last:
            last.status = 'LIVE'
            last.save(update_fields=['status'])
        from tournaments.services import sync_tournament_status
        sync_tournament_status(t)
