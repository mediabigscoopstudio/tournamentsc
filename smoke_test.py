"""End-to-end validation of the two-application architecture.

Run with:  python manage.py shell < smoke_test.py     (or: python smoke_test.py)

Drives every flow described in the brief through Django's test client against a
throwaway in-memory database, and asserts the isolation guarantees:
audience never needs auth; players, organizers and admins each have their own
door; and no role can reach another's area.
"""
import os
import sys
import django
from django.test.utils import setup_test_environment, get_runner
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tournamentsc.settings')
django.setup()
setup_test_environment()

from django.test import Client                                    # noqa: E402
from django.test.runner import DiscoverRunner                     # noqa: E402
from django.utils import timezone                                 # noqa: E402
import datetime                                                   # noqa: E402

runner = DiscoverRunner(verbosity=0, interactive=False)
old_config = runner.setup_databases()

from accounts.models import (Follow, OrganizerApplication, OrganizerProfile,  # noqa: E402
                             PlayerProfile, User)
from tournaments.services import apply_result                     # noqa: E402
from dash.models import Announcement, MediaAsset, News, SiteSetting  # noqa: E402
from tournaments.models import Sport, Team, Tournament, TournamentTeamEntry  # noqa: E402
from tournaments import constants as C                            # noqa: E402
from django.core.management import call_command                   # noqa: E402

PASS, FAIL = [], []


def check(label, cond, extra=''):
    (PASS if cond else FAIL).append(label)
    print(f'  {"PASS" if cond else "FAIL"}  {label}{"  <- " + str(extra) if extra and not cond else ""}')


def status(client, url, method='get', **kw):
    resp = getattr(client, method)(url, **kw)
    return resp.status_code, resp


def section(name):
    print(f'\n=== {name} ===')


try:
    # ---------------------------------------------------------------
    section('Seed')
    call_command('seed_sports', verbosity=0)
    call_command('seed_demo', verbosity=0)
    check('sports seeded (8, incl. pickleball)', Sport.objects.count() == 8, Sport.objects.count())
    check('pickleball exists', Sport.objects.filter(slug='pickleball').exists())
    check('tournaments seeded', Tournament.objects.count() >= 5, Tournament.objects.count())

    admin = User.objects.get(email='admin@tournamentsc.app')
    org_user = User.objects.get(email='organizer@tournamentsc.app')
    player = User.objects.filter(email='ravi.kumar@example.com').first()
    check('admin is staff', admin.is_staff)
    check('organizer is NOT staff', not org_user.is_staff)
    check('organizer approved', org_user.is_approved_organizer)
    check('player exists', player is not None)

    anon = Client()

    # ---------------------------------------------------------------
    section('Audience — every public page, no login')
    t = Tournament.objects.public().first()
    fx = t.fixtures.first()
    sport = Sport.objects.first()
    team = Team.objects.first()
    pprofile = PlayerProfile.objects.filter(user__is_suspended=False).first()

    public_urls = [
        '/', '/browse', '/browse?status=ONGOING', '/search', '/search?q=chess',
        '/live', '/results', '/highlights', '/teams', '/players', '/news',
        f'/sports/{sport.slug}',
        f'/teams/{team.pk}',
        f'/players/{pprofile.pk}',
        f'/t/{t.slug}',
        f'/t/{t.slug}/standings',
        f'/t/{t.slug}/schedule',
        f'/t/{t.slug}/m/{fx.pk}',
        f'/api/fixtures/{fx.pk}/live',
    ]
    for url in public_urls:
        code, _ = status(anon, url)
        check(f'anon GET {url} -> 200', code == 200, code)

    # ---------------------------------------------------------------
    section('Audience — login/signup choosers are public and do not authenticate')
    for url in ['/login', '/signup', '/player/login', '/player/register',
                '/organizer/login', '/organizer/register', '/dashboard/login']:
        code, _ = status(anon, url)
        check(f'anon GET {url} -> 200', code == 200, code)

    # ---------------------------------------------------------------
    section('Unauthorized access protection')
    # Anonymous hitting each area lands on THAT area's login page.
    code, r = status(anon, '/player/')
    check('anon /player/ redirects to /player/login', code == 302 and '/player/login' in r.url, r.get('Location', code))
    code, r = status(anon, '/organizer/')
    check('anon /organizer/ redirects to /organizer/login', code == 302 and '/organizer/login' in r.url, r.get('Location', code))
    code, r = status(anon, '/dashboard/')
    check('anon /dashboard/ redirects to /dashboard/login', code == 302 and '/dashboard/login' in r.url, r.get('Location', code))
    code, r = status(anon, '/dashboard/users/')
    check('anon /dashboard/users/ redirects to admin login', code == 302 and '/dashboard/login' in r.url, r.get('Location', code))

    # Draft tournaments are invisible to the audience.
    draft = Tournament.objects.create(
        name='Hidden Draft Cup', sport=sport, organizer=org_user.organizer_profile,
        format=sport.allowed_formats[0], start_date=timezone.now().date(),
        end_date=timezone.now().date(), status='DRAFT')
    code, _ = status(anon, f'/t/{draft.slug}')
    check('anon cannot see a DRAFT tournament (404)', code == 404, code)

    # ---------------------------------------------------------------
    section('Player flow')
    pc = Client()
    code, r = status(pc, '/player/login', 'post',
                     data={'identifier': 'ravi.kumar@example.com', 'password': 'player12345'})
    check('player login succeeds -> /player/', code == 302 and r.url == '/player/', r.get('Location', code))

    for url in ['/player/', '/player/tournaments', '/player/schedule', '/player/results',
                '/player/profile', '/notifications']:
        code, _ = status(pc, url)
        check(f'player GET {url} -> 200', code == 200, code)

    # Player cannot reach organizer or admin areas.
    code, r = status(pc, '/organizer/')
    check('player -> /organizer/ bounced to organizer_status', code == 302 and 'status' in r.url, r.get('Location', code))
    code, _ = status(pc, '/dashboard/')
    check('player -> /dashboard/ = 403 (never a redirect)', code == 403, code)
    code, _ = status(pc, '/dashboard/users/')
    check('player -> /dashboard/users/ = 403', code == 403, code)
    code, _ = status(pc, f'/organizer/t/{t.slug}/', 'get')
    check('player -> another org\'s manage page bounced', code in (302, 403), code)

    # Player joins an open individual tournament. (Every seeded tournament has
    # already been played to COMPLETED, and a completed event correctly refuses
    # entries — so create one that is genuinely open.)
    _today = timezone.now().date()
    solo = Tournament.objects.create(
        name='Open Marathon', sport=Sport.objects.get(slug='marathon'),
        organizer=org_user.organizer_profile, format=C.FORMAT_SINGLE_EVENT,
        city='Bhubaneswar', start_date=_today,
        end_date=_today + datetime.timedelta(days=1), status='PUBLISHED')
    code, r = status(pc, f'/player/join/{solo.slug}')
    check('player GET join page -> 200', code == 200, code)
    before = solo.registrations.count()
    code, r = status(pc, f'/player/join/{solo.slug}', 'post', data={})
    check('player POST join -> redirect', code == 302, code)
    check('player registration recorded', solo.registrations.count() > before,
          solo.registrations.count())

    # A completed tournament refuses new entries.
    done = Tournament.objects.filter(status='COMPLETED').first()
    if done:
        code, r = status(pc, f'/player/join/{done.slug}')
        check('completed tournament refuses entries', code == 302, code)

    # ---------------------------------------------------------------
    section('Player registration (new account)')
    nc = Client()
    code, r = status(nc, '/player/register', 'post', data={
        'first_name': 'Test Player', 'email': 'newplayer@example.com',
        'phone_number': '', 'password1': 'strongpass123', 'password2': 'strongpass123'})
    check('player signup -> /player/', code == 302 and r.url == '/player/', r.get('Location', code))
    np = User.objects.filter(email='newplayer@example.com').first()
    check('new player created', np is not None)
    check('new player is NOT staff', np and not np.is_staff)
    check('new player has PlayerProfile', np and hasattr(np, 'player_profile'))

    # ---------------------------------------------------------------
    section('Organizer flow')
    oc = Client()
    code, r = status(oc, '/organizer/login', 'post',
                     data={'identifier': 'organizer@tournamentsc.app', 'password': 'organizer12345'})
    check('organizer login -> /organizer/', code == 302 and r.url == '/organizer/', r.get('Location', code))

    for url in ['/organizer/', '/organizer/tournaments/new', '/organizer/profile']:
        code, _ = status(oc, url)
        check(f'organizer GET {url} -> 200', code == 200, code)

    # Organizer must never reach the admin console.
    code, _ = status(oc, '/dashboard/')
    check('organizer -> /dashboard/ = 403', code == 403, code)
    code, _ = status(oc, '/dashboard/settings/')
    check('organizer -> /dashboard/settings/ = 403', code == 403, code)
    code, _ = status(oc, '/dashboard/users/')
    check('organizer -> global users = 403', code == 403, code)

    # Organizer cannot sign in at the admin door.
    bad = Client()
    code, r = status(bad, '/dashboard/login', 'post',
                     data={'identifier': 'organizer@tournamentsc.app', 'password': 'organizer12345'})
    check('organizer rejected at /dashboard/login (no redirect)', code == 200, code)
    code, _ = status(bad, '/dashboard/')
    check('...and is still not authenticated there', code == 302, code)

    # Admin cannot sign in at the player/organizer doors.
    bad2 = Client()
    code, _ = status(bad2, '/player/login', 'post',
                     data={'identifier': 'admin@tournamentsc.app', 'password': 'admin12345'})
    check('admin rejected at /player/login', code == 200, code)
    code, _ = status(bad2, '/organizer/login', 'post',
                     data={'identifier': 'admin@tournamentsc.app', 'password': 'admin12345'})
    check('admin rejected at /organizer/login', code == 200, code)

    # ---- Tournament CRUD (organizer) ----
    today = timezone.now().date()
    code, r = status(oc, '/organizer/tournaments/new', 'post', data={
        'name': 'E2E Cup', 'sport': Sport.objects.get(slug='basketball').id,
        'format': C.FORMAT_KNOCKOUT, 'description': 'test',
        'city': 'Bhubaneswar', 'start_date': str(today),
        'end_date': str(today + datetime.timedelta(days=2)),
        'registration_deadline': '', 'entry_fee': '', 'max_participants': '',
        'youtube_url': ''})
    e2e = Tournament.objects.filter(name='E2E Cup').first()
    check('organizer created tournament', e2e is not None, r.content[:300] if e2e is None else '')
    check('new tournament starts as DRAFT', e2e and e2e.status == 'DRAFT')

    # Team CRUD
    code, _ = status(oc, f'/organizer/t/{e2e.slug}/participants')
    check('participants page -> 200', code == 200, code)
    for nm in ['Alpha FC', 'Beta FC']:
        status(oc, f'/organizer/t/{e2e.slug}/participants/team/add', 'post', data={'name': nm})
    check('2 teams added', e2e.team_entries.count() == 2, e2e.team_entries.count())

    # Approve/reject a team entry
    entry = e2e.team_entries.first()
    status(oc, f'/organizer/t/{e2e.slug}/participants/team/{entry.id}/reject', 'post')
    entry.refresh_from_db()
    check('organizer can reject a team entry', entry.status == 'REJECTED', entry.status)
    status(oc, f'/organizer/t/{e2e.slug}/participants/team/{entry.id}/approve', 'post')
    entry.refresh_from_db()
    check('organizer can approve a team entry', entry.status == 'APPROVED', entry.status)

    # Basketball is knockout format -> fixtures are now arranged by the organiser
    # (Seed/Random auto-generation was replaced by manual fixture creation).
    team_ids = list(e2e.team_entries.filter(status='APPROVED').values_list('team_id', flat=True))
    code, r = status(oc, f'/organizer/t/{e2e.slug}/fixtures/add-manual', 'post', data={
        'entrant_a': f'team:{team_ids[0]}', 'entrant_b': f'team:{team_ids[1]}'})
    e2e.refresh_from_db()
    check('organiser-created fixture saved', e2e.fixtures.count() >= 1, e2e.fixtures.count())
    check('creating a fixture published the tournament', e2e.status == 'PUBLISHED', e2e.status)

    efx = e2e.fixtures.first()
    for url in [f'/organizer/t/{e2e.slug}/fixtures',
                f'/organizer/t/{e2e.slug}/fixtures/{efx.id}/score',
                f'/organizer/t/{e2e.slug}/fixtures/{efx.id}/schedule',
                f'/organizer/t/{e2e.slug}/fixtures/{efx.id}/highlight',
                f'/organizer/t/{e2e.slug}/edit',
                f'/organizer/t/{e2e.slug}/']:
        code, _ = status(oc, url)
        check(f'organizer GET {url} -> 200', code == 200, code)

    # Live scoring
    status(oc, f'/organizer/t/{e2e.slug}/fixtures/{efx.id}/score', 'post', data={'action': 'start'})
    efx.refresh_from_db()
    check('fixture marked LIVE', efx.status == 'LIVE', efx.status)
    code, r = status(anon, f'/api/fixtures/{efx.id}/live')
    check('public live API serves the live fixture', code == 200 and r.json()['status'] == 'LIVE')

    parts = list(efx.ordered_participants())
    if len(parts) == 2:
        status(oc, f'/organizer/t/{e2e.slug}/fixtures/{efx.id}/score', 'post', data={
            'action': 'finalize',
            f'score_{parts[0].id}': '80', f'score_{parts[1].id}': '70'})
        efx.refresh_from_db()
        check('fixture finalized', efx.status == 'COMPLETED', efx.status)

    # Ownership: another organizer cannot manage this tournament
    other = User.objects.create_user(email='other-org@example.com', password='pass12345678',
                                     first_name='Other Org')
    op = OrganizerProfile.objects.create(user=other, is_approved=True,
                                         approved_at=timezone.now())
    oc2 = Client()
    oc2.post('/organizer/login', {'identifier': 'other-org@example.com', 'password': 'pass12345678'})
    code, _ = status(oc2, f'/organizer/t/{e2e.slug}/')
    check("organizer cannot manage another organizer's tournament (403)", code == 403, code)

    # ---------------------------------------------------------------
    section('Organizer registration + admin approval')
    rc = Client()
    code, r = status(rc, '/organizer/register', 'post', data={
        'first_name': 'New Org', 'email': 'neworg@example.com', 'phone_number': '',
        'organization_name': 'New Club',
        'password1': 'strongpass123', 'password2': 'strongpass123'})
    check('organizer signup -> /organizer/apply', code == 302 and 'apply' in r.url, r.get('Location', code))
    no = User.objects.filter(email='neworg@example.com').first()
    check('new organizer is NOT staff', no and not no.is_staff)
    check('new organizer NOT auto-approved', no and not no.is_approved_organizer)

    code, r = status(rc, '/organizer/apply', 'post', data={
        'affiliation': 'New Club', 'reason': 'I run local leagues.',
        'sports': [Sport.objects.first().id]})
    check('application submitted', OrganizerApplication.objects.filter(user=no).exists())

    # Unapproved organizer cannot create tournaments.
    code, r = status(rc, '/organizer/tournaments/new')
    check('unapproved organizer blocked from creating', code == 302 and 'status' in r.url, r.get('Location', code))

    # ---------------------------------------------------------------
    section('Admin application — login + every section')
    ac = Client()
    code, r = status(ac, '/dashboard/login', 'post',
                     data={'identifier': 'admin@tournamentsc.app', 'password': 'admin12345'})
    check('admin login -> /dashboard/', code == 302 and r.url == '/dashboard/', r.get('Location', code))

    from dash.resources import RESOURCES
    admin_pages = ['/dashboard/', '/dashboard/analytics/', '/dashboard/reports/',
                   '/dashboard/logs/', '/dashboard/permissions/', '/dashboard/settings/']
    for url in admin_pages:
        code, _ = status(ac, url)
        check(f'admin GET {url} -> 200', code == 200, code)

    for key in RESOURCES:
        code, _ = status(ac, f'/dashboard/{key}/')
        check(f'admin GET /dashboard/{key}/ -> 200', code == 200, code)

    for key, res in RESOURCES.items():
        if res.can_create and res.form:
            code, _ = status(ac, f'/dashboard/{key}/new')
            check(f'admin GET /dashboard/{key}/new -> 200', code == 200, code)

    # Edit form for every resource that has a row
    for key, res in RESOURCES.items():
        if not (res.can_edit and res.form):
            continue
        obj = res.model._default_manager.first()
        if obj is None:
            continue
        code, _ = status(ac, f'/dashboard/{key}/{obj.pk}/edit')
        check(f'admin GET /dashboard/{key}/{obj.pk}/edit -> 200', code == 200, code)

    # CSV export for every resource
    for key in RESOURCES:
        code, r = status(ac, f'/dashboard/reports/{key}.csv')
        check(f'admin CSV export {key} -> 200', code == 200 and r['Content-Type'] == 'text/csv', code)

    # ---------------------------------------------------------------
    section('Admin CRUD')
    # Create
    code, r = status(ac, '/dashboard/news/new', 'post', data={
        'title': 'Season opener', 'slug': '', 'summary': 'It begins',
        'body': 'Full story here.', 'sport': '', 'tournament': '',
        'status': 'PUBLISHED', 'published_at': '', 'is_archived': False})
    art = News.objects.filter(title='Season opener').first()
    check('admin created a news article', art is not None, r.content[:300] if art is None else '')
    check('publishing without a date auto-stamps it', art and art.published_at is not None)
    check('published article is public', anon.get(f'/news/{art.slug}').status_code == 200)

    # Update
    status(ac, f'/dashboard/news/{art.pk}/edit', 'post', data={
        'title': 'Season opener (updated)', 'slug': art.slug, 'summary': 'It begins',
        'body': 'Full story here.', 'sport': '', 'tournament': '',
        'status': 'PUBLISHED', 'published_at': '', 'is_archived': False})
    art.refresh_from_db()
    check('admin updated the article', art.title == 'Season opener (updated)', art.title)

    # Actions: unpublish / archive / restore / publish
    status(ac, f'/dashboard/news/{art.pk}/unpublish', 'post')
    art.refresh_from_db()
    check('admin unpublished it', art.status == 'DRAFT', art.status)
    check('unpublished article is 404 to the public', anon.get(f'/news/{art.slug}').status_code == 404)
    status(ac, f'/dashboard/news/{art.pk}/publish', 'post')
    art.refresh_from_db()
    check('admin re-published it', art.is_live)
    status(ac, f'/dashboard/news/{art.pk}/archive', 'post')
    art.refresh_from_db()
    check('admin archived it', art.is_archived)
    status(ac, f'/dashboard/news/{art.pk}/restore', 'post')
    art.refresh_from_db()
    check('admin restored it', not art.is_archived)

    # Delete
    status(ac, f'/dashboard/news/{art.pk}/delete', 'post')
    check('admin deleted the article', not News.objects.filter(pk=art.pk).exists())

    # Admin manages ORGANIZER-created content
    status(ac, f'/dashboard/tournaments/{e2e.pk}/archive', 'post')
    e2e.refresh_from_db()
    check('admin archived an organizer\'s tournament', e2e.is_removed)
    check('archived tournament hidden from public', anon.get(f'/t/{e2e.slug}').status_code == 404)
    status(ac, f'/dashboard/tournaments/{e2e.pk}/restore', 'post')
    e2e.refresh_from_db()
    check('admin restored it', not e2e.is_removed)
    check('restored tournament public again', anon.get(f'/t/{e2e.slug}').status_code == 200)

    status(ac, f'/dashboard/tournaments/{e2e.pk}/feature', 'post')
    e2e.refresh_from_db()
    check('admin featured a tournament', e2e.is_featured)

    # Admin approves the pending organizer application
    app = OrganizerApplication.objects.filter(user=no, status='PENDING').first()
    status(ac, f'/dashboard/applications/{app.pk}/approve', 'post')
    app.refresh_from_db()
    no.refresh_from_db()
    check('admin approved the application', app.status == 'APPROVED', app.status)
    check('...which granted organizer capability', no.is_approved_organizer)
    code, _ = status(rc, '/organizer/tournaments/new')
    check('newly approved organizer can now create', code == 200, code)

    # Admin suspends a user -> they are logged out on the next request
    status(ac, f'/dashboard/users/{player.pk}/suspend', 'post', data={'reason': 'testing'})
    player.refresh_from_db()
    check('admin suspended the player', player.is_suspended)
    code, r = status(pc, '/player/')
    check('suspended user is bounced on their next request', code == 302, code)
    code, r = status(Client(), '/player/login')
    status(ac, f'/dashboard/users/{player.pk}/activate', 'post')
    player.refresh_from_db()
    check('admin reactivated the player', not player.is_suspended)

    # Admin cannot suspend themselves / a superuser
    status(ac, f'/dashboard/users/{admin.pk}/suspend', 'post', data={'reason': 'oops'})
    admin.refresh_from_db()
    check('admin cannot suspend a superuser (self-lockout guard)', not admin.is_suspended)

    # Admin creates another admin — the only way is_staff can be granted
    code, r = status(ac, '/dashboard/users/new', 'post', data={
        'first_name': 'Second', 'last_name': 'Admin', 'email': 'admin2@tournamentsc.app',
        'phone_number': '', 'password1': 'adminpass1234',
        'is_platform_admin': 'on', 'is_active': 'on', 'suspended_reason': ''})
    a2 = User.objects.filter(email='admin2@tournamentsc.app').first()
    check('admin created another administrator', a2 is not None and a2.is_staff,
          r.content[:400] if a2 is None else '')
    c2 = Client()
    code, r = status(c2, '/dashboard/login', 'post',
                     data={'identifier': 'admin2@tournamentsc.app', 'password': 'adminpass1234'})
    check('the new administrator can sign in at the admin door', code == 302, code)

    # ---------------------------------------------------------------
    section('Settings drive real behaviour')
    conf = SiteSetting.load()
    conf.allow_player_registration = False
    conf.save()
    code, r = status(Client(), '/player/register')
    check('closing player registration blocks the signup page', code == 302, code)
    conf.allow_player_registration = True
    conf.save()
    code, _ = status(Client(), '/player/register')
    check('re-opening it restores the page', code == 200, code)

    ann = Announcement.objects.create(title='Heads up', message='Finals this weekend.',
                                      status='PUBLISHED', published_at=timezone.now())
    body = anon.get('/').content.decode()
    check('a published announcement shows on the public site', 'Finals this weekend.' in body)
    check('...but never inside the admin console',
          'Finals this weekend.' not in ac.get('/dashboard/').content.decode())
    ann.is_archived = True
    ann.save()
    body = anon.get('/').content.decode()
    check('an archived announcement disappears', 'Finals this weekend.' not in body)

    conf.maintenance_mode = True
    conf.maintenance_message = 'Back at 6pm.'
    conf.save()
    check('maintenance mode shows a public notice', 'Back at 6pm.' in anon.get('/').content.decode())
    conf.maintenance_mode = False
    conf.save()

    # ---------------------------------------------------------------
    section('File uploads (media root)')
    from django.core.files.uploadedfile import SimpleUploadedFile

    # A 1x1 GIF — enough to exercise FileField storage end to end.
    gif = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!'
           b'\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00'
           b'\x00\x02\x02D\x01\x00;')
    code, r = status(ac, '/dashboard/media/new', 'post', data={
        'title': 'Test asset', 'kind': 'image',
        'file': SimpleUploadedFile('t.gif', gif, content_type='image/gif'),
        'external_url': '', 'alt_text': 'test', 'tournament': '', 'is_archived': False})
    asset = MediaAsset.objects.filter(title='Test asset').first()
    check('admin uploaded a media asset', asset is not None and bool(asset.file),
          r.content[:300] if asset is None else '')
    check('...stored under the library/ prefix', asset and 'library/' in asset.file.name)
    check('...and the uploader was stamped', asset and asset.uploaded_by_id == admin.id)

    # A media asset needs either a file or a URL — neither must be rejected.
    code, r = status(ac, '/dashboard/media/new', 'post', data={
        'title': 'Empty asset', 'kind': 'image', 'external_url': '',
        'alt_text': '', 'tournament': '', 'is_archived': False})
    check('media with neither file nor URL is rejected',
          code == 200 and not MediaAsset.objects.filter(title='Empty asset').exists())

    # Organizer uploads a tournament banner.
    code, r = status(oc, f'/organizer/t/{e2e.slug}/edit', 'post', data={
        'name': e2e.name, 'sport': e2e.sport_id, 'format': e2e.format,
        'description': 'with a banner', 'city': 'Bhubaneswar',
        'start_date': str(e2e.start_date), 'end_date': str(e2e.end_date),
        'registration_deadline': '', 'entry_fee': '', 'max_participants': '',
        'youtube_url': '',
        'banner_image': SimpleUploadedFile('b.gif', gif, content_type='image/gif')})
    e2e.refresh_from_db()
    check('organizer uploaded a tournament banner', bool(e2e.banner_image), r.status_code)
    check('...stored under tournaments/', 'tournaments/' in (e2e.banner_image.name or ''))

    # ---------------------------------------------------------------
    section('BUG FIX: scores must compare as numbers, not strings')
    # '9' > '11' lexicographically. The bracket engine used to compare the raw
    # posted strings, so the competitor who scored 9 beat the one who scored 11.
    from tournaments.models import Fixture, FixtureParticipant
    from tournaments.engines import BracketEngine

    bug = Tournament.objects.create(
        name='String Compare Cup', sport=Sport.objects.get(slug='basketball'),
        organizer=org_user.organizer_profile, format=C.FORMAT_KNOCKOUT,
        start_date=_today, end_date=_today, status='PUBLISHED')
    for nm in ['Nine FC', 'Eleven FC']:
        team = Team.objects.create(name=nm, sport=bug.sport)
        TournamentTeamEntry.objects.create(tournament=bug, team=team)
    bug.engine.generate_fixtures()
    bfx = bug.fixtures.first()
    bp = list(bfx.participants.all())
    nine = next(p for p in bp if p.team.name == 'Nine FC')
    eleven = next(p for p in bp if p.team.name == 'Eleven FC')

    # Post them exactly as the scoring form does — as strings.
    apply_result(bfx, {'finalize': True,
                       str(nine.id): {'score': '9'},
                       str(eleven.id): {'score': '11'}})
    nine.refresh_from_db(); eleven.refresh_from_db()
    check('11 beats 9 (not the other way round)', eleven.is_winner is True and not nine.is_winner,
          f'nine.is_winner={nine.is_winner} eleven.is_winner={eleven.is_winner}')

    # A draw must not produce a winner, and must not advance anyone.
    draw_t = Tournament.objects.create(
        name='Draw Cup', sport=Sport.objects.get(slug='basketball'),
        organizer=org_user.organizer_profile, format=C.FORMAT_KNOCKOUT,
        start_date=_today, end_date=_today, status='PUBLISHED')
    for nm in ['Alpha', 'Beta']:
        TournamentTeamEntry.objects.create(
            tournament=draw_t, team=Team.objects.create(name=nm + ' DC', sport=draw_t.sport))
    draw_t.engine.generate_fixtures()
    dfx = draw_t.fixtures.first()
    dp = list(dfx.participants.all())
    apply_result(dfx, {'finalize': True,
                       str(dp[0].id): {'score': '10'}, str(dp[1].id): {'score': '10'}})
    dfx.refresh_from_db()
    check('a drawn knockout has no winner', not any(
        p.is_winner for p in dfx.participants.all()))

    # ---------------------------------------------------------------
    section('Reference parity — new features')
    bball = Tournament.objects.get(name='City Basketball Open')
    check('prize pool stored', bball.prize_pool == 40000, bball.prize_pool)
    check('venue stored', bball.venue is not None and 'Koramangala' in bball.venue.name)
    check('rules stored', bool(bball.rules))
    body = anon.get(f'/t/{bball.slug}').content.decode()
    check('prize pool renders on the tournament page', '40000' in body or '40,000' in body)
    check('venue renders on the tournament page', 'Koramangala' in body)
    check('rules render on the tournament page', 'Rules' in body)
    check('Follow button renders for the audience', 'Follow' in body)

    # Chess: rating + Buchholz
    chess = Tournament.objects.get(name='GDG Chess Open')
    st = list(chess.standings.all())
    check('chess standings computed', len(st) == 4, len(st))
    check('Buchholz computed', all('buchholz' in (s.extra_stats or {}) for s in st))
    check('rating carried into standings', any((s.extra_stats or {}).get('rating') for s in st))
    cbody = anon.get(f'/t/{chess.slug}').content.decode()
    check('Buchholz column renders', 'Buchholz' in cbody)
    check('Rating column renders', 'Rating' in cbody)

    # Esports: placement + kills breakdown
    espt = Tournament.objects.get(name='BGMI City Scrims')
    est = list(espt.standings.all())
    check('esports standings computed', len(est) > 0)
    check('kills persisted in standings', any((s.extra_stats or {}).get('kills') for s in est))
    ebody = anon.get(f'/t/{espt.slug}').content.decode()
    check('esports leaderboard shows Kills column', 'Kills' in ebody)
    check('esports leaderboard shows Place pts column', 'Place pts' in ebody)

    # Marathon: bib + pace (needs a category distance)
    mara = Tournament.objects.get(name='Kalinga Half Marathon')
    mfx = mara.fixtures.first()
    mp = list(mfx.ordered_participants())
    check('bib denormalised onto the participant', bool(mp[0].bib), mp[0].stats)
    check('pace computed from category distance', bool(mp[0].pace_display), mp[0].pace_display)
    mbody = anon.get(f'/t/{mara.slug}').content.decode()
    check('marathon leaderboard shows Bib', 'Bib' in mbody)
    check('marathon leaderboard shows Pace', 'Pace' in mbody)
    check('time renders as a clock, not raw ms', ' ms' not in mbody)

    # Highlights: duration + views
    from tournaments.models import Highlight as HL
    hl = HL.objects.filter(duration_seconds__isnull=False).first()
    check('highlight has a duration', hl is not None)
    check('duration formats as m:ss', hl and ':' in hl.duration_display, hl.duration_display if hl else '')
    check('views format as k', hl and hl.views_display.endswith('k'), hl.views_display if hl else '')
    before_views = hl.view_count
    anon.get(hl.fixture.get_absolute_url())
    hl.refresh_from_db()
    check('viewing a match increments the highlight view count',
          hl.view_count == before_views + 1, hl.view_count)

    # Win probability
    livefx = Fixture.objects.filter(status='LIVE').first()
    if livefx:
        wp = livefx.win_probability
        check('win probability is a sane percentage', wp is None or 0 < wp < 100, wp)
        if wp:
            check('complement is exposed for templates',
                  livefx.win_probability_other == 100 - wp)

    # Live API carries the new fields
    api = anon.get(f'/api/fixtures/{mfx.id}/live').json()
    check('live API exposes bib', 'bib' in api['participants'][0])
    check('live API exposes pace', 'pace' in api['participants'][0])
    check('live API exposes win_probability', 'win_probability' in api)
    check('live API time is a clock', not str(api['participants'][0].get('time') or '').endswith('ms'))

    # ---------------------------------------------------------------
    section('Follow feature (end to end)')
    # The suspension test above logged this client out (correctly). Sign back in.
    pc = Client()
    pc.post('/player/login', {'identifier': 'ravi.kumar@example.com', 'password': 'player12345'})
    check('player re-authenticated after reinstatement',
          pc.get('/player/').status_code == 200)

    code, r = status(pc, f'/player/follow/{bball.slug}', 'post')
    check('player can follow a tournament', code == 302 and
          Follow.objects.filter(user=player, tournament=bball).exists(), code)
    code, _ = status(pc, '/player/following')
    check('following page renders', code == 200, code)
    check('followed tournament listed',
          bball.name in pc.get('/player/following').content.decode())

    # Following again unfollows (toggle).
    status(pc, f'/player/follow/{bball.slug}', 'post')
    check('following again unfollows',
          not Follow.objects.filter(user=player, tournament=bball).exists())

    # A follower is notified when a result lands.
    Follow.objects.create(user=player, tournament=bball)
    n_before = player.notifications.count()
    pending = bball.fixtures.filter(status='SCHEDULED').first()
    if pending:
        pp = list(pending.participants.all())
        if len(pp) == 2 and all(x.team or x.player for x in pp):
            apply_result(pending, {'finalize': True,
                                   str(pp[0].id): {'score': '30'},
                                   str(pp[1].id): {'score': '20'}})
            check('a follower is notified when a result is posted',
                  player.notifications.count() > n_before,
                  f'{n_before} -> {player.notifications.count()}')

    # Anonymous cannot follow.
    code, r = status(anon, f'/player/follow/{bball.slug}', 'post')
    check('anonymous cannot follow (sent to player login)',
          code == 302 and '/player/login' in r.url, r.get('Location', code))
    # Admin cannot follow — they live in the console.
    code, r = status(ac, f'/player/follow/{bball.slug}', 'post')
    check('admin cannot follow', code == 302 and 'dashboard' in r.url, r.get('Location', code))

    # ---------------------------------------------------------------
    section('Admin stays in sync with the new features')
    code, _ = status(ac, '/dashboard/follows/')
    check('admin has a Followers section', code == 200, code)
    abody = ac.get('/dashboard/tournaments/').content.decode()
    check('admin tournament list shows Prize pool', 'Prize pool' in abody)
    check('admin tournament list shows Followers', 'Followers' in abody)
    hbody = ac.get('/dashboard/highlights/').content.decode()
    check('admin highlights list shows Views', 'Views' in hbody)
    cbody2 = ac.get('/dashboard/categories/').content.decode()
    check('admin categories list shows Distance', 'Distance' in cbody2)
    anbody = ac.get('/dashboard/analytics/').content.decode()
    check('analytics shows follows + highlight views', 'Tournament follows' in anbody
          and 'Highlight views' in anbody)

    # Admin can edit the new fields through the generic CRUD form.
    fbody = ac.get(f'/dashboard/tournaments/{bball.pk}/edit').content.decode()
    check('admin edit form exposes prize_pool', 'prize_pool' in fbody)
    check('admin edit form exposes rules', 'name="rules"' in fbody)
    check('admin edit form exposes venue', 'name="venue"' in fbody)

    # Organizer form exposes them too.
    obody = oc.get(f'/organizer/t/{e2e.slug}/edit').content.decode()
    check('organizer edit form exposes prize_pool', 'prize_pool' in obody)
    check('organizer edit form exposes venue', 'name="venue"' in obody)
    check('organizer edit form exposes rules', 'name="rules"' in obody)

    # ---------------------------------------------------------------
    section('Django admin is not exposed')
    for url in ['/admin/', '/admin/login/', '/django-admin/']:
        code, _ = status(anon, url)
        check(f'{url} is not routed (404)', code == 404, code)
    check('django.contrib.admin not installed', 'django.contrib.admin' not in settings.INSTALLED_APPS)

    # ---------------------------------------------------------------
    section('Templates: no comment leaks into rendered HTML')
    # Django's {# #} is SINGLE-LINE only. A multi-line one is not a comment —
    # its text renders onto the page. Catch it at the source.
    import pathlib as _pl
    import re as _re
    leaks = []
    for _p in _pl.Path('template').rglob('*.html'):
        for _i, _line in enumerate(_p.read_text(encoding='utf-8').splitlines(), 1):
            if '{#' in _line and '#}' not in _line.split('{#', 1)[1]:
                leaks.append(f'{_p}:{_i}')
    check('no multi-line {# #} comments (they render as visible text)',
          not leaks, leaks)

    # And prove it end to end on the pages that had them.
    for _u, _needle in [('/browse', 'querystring'),
                        (f'/t/{chess.slug}', 'Context:'),
                        (f'/t/{mara.slug}', 'Shared by')]:
        body_ = anon.get(_u).content.decode()
        check(f'{_u} renders no leaked comment text', _needle not in body_)
    check('404 page renders no leaked comment text',
          'Self-contained' not in anon.get('/no-such-page-xyz').content.decode())

    # ---------------------------------------------------------------
    section('No broken {% url %} / reverse() anywhere')
    from django.urls import reverse, NoReverseMatch
    from django.urls.exceptions import NoReverseMatch as NRM
    check('reverse(home)', reverse('home') == '/')
    check('reverse(dash_index)', reverse('dash_index') == '/dashboard/')
    check('reverse(player_login)', reverse('player_login') == '/player/login')
    check('reverse(organizer_login)', reverse('organizer_login') == '/organizer/login')

finally:
    print('\n' + '=' * 60)
    print(f'PASSED: {len(PASS)}    FAILED: {len(FAIL)}')
    if FAIL:
        print('\nFailures:')
        for f in FAIL:
            print('  -', f)
    print('=' * 60)
    runner.teardown_databases(old_config)
    sys.exit(1 if FAIL else 0)
