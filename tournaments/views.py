from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import approved_organizer_required, player_required
from accounts.models import Follow, Notification, PlayerProfile
from . import constants as C
from .forms import (FixtureScheduleForm, HighlightForm, IndividualEntryForm, TeamForm,
                    TournamentForm)
from .models import (Fixture, FixtureParticipant, IndividualRegistration, ScoreEvent, Team,
                     TeamMembership, Tournament, TournamentTeamEntry)
from .services import apply_result, sync_tournament_status
from .utils import format_ms, parse_time_to_ms


# ======================================================================
# Ownership guard
# ======================================================================
def _owned(request, slug):
    """An organizer may only manage tournaments they own.

    Platform admins do NOT get a back door here — they manage every tournament
    from the admin console. Keeping this check strict is what makes the two
    applications genuinely separate.
    """
    t = get_object_or_404(Tournament, slug=slug)
    if t.organizer.user_id != request.user.id:
        raise PermissionDenied('You do not manage this tournament.')
    return t


# ======================================================================
# Organizer dashboard
# ======================================================================
@approved_organizer_required
def organizer_dashboard(request):
    tournaments = request.user.organizer_profile.tournaments.select_related('sport').all()
    pending_members = TeamMembership.objects.filter(
        team__entries__tournament__organizer=request.user.organizer_profile,
        is_approved=False).count()
    pending_entries = TournamentTeamEntry.objects.filter(
        tournament__organizer=request.user.organizer_profile, status='PENDING').count()
    return render(request, 'organizer/dashboard.html', {
        'tournaments': tournaments,
        'counts': {
            'total': tournaments.count(),
            'live': tournaments.filter(status='ONGOING').count(),
            'draft': tournaments.filter(status='DRAFT').count(),
            'pending_requests': pending_members + pending_entries,
        },
    })


@approved_organizer_required
def tournament_create(request):
    form = TournamentForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        t = form.save(commit=False)
        t.organizer = request.user.organizer_profile
        t.save()
        messages.success(request, 'Tournament created as a draft. Add participants next.')
        return redirect('tournament_manage', slug=t.slug)
    return render(request, 'organizer/tournament_form.html', {'form': form, 'create': True})


@approved_organizer_required
def tournament_edit(request, slug):
    t = _owned(request, slug)
    form = TournamentForm(request.POST or None, request.FILES or None, instance=t,
                          locked=t.fixtures_generated)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Tournament updated.')
        return redirect('tournament_manage', slug=t.slug)
    return render(request, 'organizer/tournament_form.html',
                  {'form': form, 'create': False, 'tournament': t})


@approved_organizer_required
def tournament_manage(request, slug):
    t = _owned(request, slug)
    return render(request, 'organizer/manage.html', {
        'tournament': t,
        'participant_count': t.participant_count(),
        'fixtures': t.fixtures.filter(is_removed=False).select_related('event_category'),
        'manual_mode': _manual_fixtures_supported(t),
        'pending_team_members': TeamMembership.objects.filter(
            team__entries__tournament=t, is_approved=False).select_related('team', 'player__user'),
    })


@approved_organizer_required
@require_POST
def tournament_publish(request, slug):
    """Take a draft live. Publishing is the organizer's own switch — the admin
    can still unpublish or archive it from the console."""
    t = _owned(request, slug)
    if t.status != 'DRAFT':
        messages.info(request, f'"{t.name}" is already published.')
    elif t.participant_count() < 2:
        messages.error(request, 'Add at least two participants before publishing.')
    else:
        t.status = 'PUBLISHED'
        t.save(update_fields=['status', 'updated_at'])
        messages.success(request, f'"{t.name}" is now public.')
    return redirect('tournament_manage', slug=slug)


@approved_organizer_required
@require_POST
def tournament_cancel(request, slug):
    t = _owned(request, slug)
    t.status = 'CANCELLED'
    t.save(update_fields=['status', 'updated_at'])
    messages.warning(request, f'"{t.name}" has been cancelled.')
    return redirect('organizer_dashboard')


@approved_organizer_required
@require_POST
def tournament_delete(request, slug):
    """Organizers may delete their *own* tournament, and only while it is still a
    draft — deleting one that has run would destroy results the audience saw."""
    t = _owned(request, slug)
    if t.status != 'DRAFT':
        messages.error(request, 'Only a draft can be deleted. Cancel it instead.')
        return redirect('tournament_manage', slug=slug)
    name = t.name
    t.delete()
    messages.success(request, f'Deleted draft "{name}".')
    return redirect('organizer_dashboard')


# ======================================================================
# Participants / registrations
# ======================================================================
@approved_organizer_required
def participants_manage(request, slug):
    t = _owned(request, slug)
    ctx = {'tournament': t}
    if t.is_team_based:
        ctx['entries'] = t.team_entries.select_related('team').prefetch_related(
            Prefetch('team__memberships',
                     queryset=TeamMembership.objects.select_related('player__user')))
        ctx['team_form'] = TeamForm()
    else:
        ctx['registrations'] = t.registrations.select_related('player__user', 'event_category')
        ctx['entry_form'] = IndividualEntryForm(tournament=t)
    return render(request, 'organizer/participants.html', ctx)


@approved_organizer_required
@require_POST
def team_add(request, slug):
    t = _owned(request, slug)
    form = TeamForm(request.POST, request.FILES)
    if form.is_valid():
        team = form.save(commit=False)
        team.sport = t.sport
        team.save()
        TournamentTeamEntry.objects.get_or_create(tournament=t, team=team,
                                                  defaults={'status': 'APPROVED'})
        messages.success(request, f'Team "{team.name}" added.')
    else:
        messages.error(request, 'Could not add team — check the name.')
    return redirect('participants_manage', slug=slug)


@approved_organizer_required
@require_POST
def teams_bulk_import(request, slug):
    """Bulk-add teams from a CSV/.xlsx of team names — same "Add Team"
    section, using the exact Team + TournamentTeamEntry creation flow as the
    manual form above, so imported teams show up identically in the list."""
    t = _owned(request, slug)
    if not t.is_team_based:
        messages.error(request, 'Team import is only available for team-based tournaments.')
        return redirect('participants_manage', slug=slug)

    upload = request.FILES.get('teams_file')
    if not upload:
        messages.error(request, 'Choose a CSV or .xlsx file to import.')
        return redirect('participants_manage', slug=slug)

    from .team_import import parse_teams
    parsed = parse_teams(upload)

    added = 0
    for name in parsed.names:
        team = Team.objects.create(name=name, sport=t.sport)
        TournamentTeamEntry.objects.get_or_create(tournament=t, team=team,
                                                  defaults={'status': 'APPROVED'})
        added += 1

    if added:
        messages.success(request, f'Imported {added} team(s).')
    if parsed.skipped_blank:
        messages.info(request, f'{parsed.skipped_blank} empty row(s) were skipped.')
    for err in parsed.errors:
        messages.error(request, err)
    if not added and not parsed.errors:
        messages.error(request, 'No teams were imported — the file had no valid rows.')

    return redirect('participants_manage', slug=slug)


@approved_organizer_required
@require_POST
def team_member_add(request, slug, team_id):
    t = _owned(request, slug)
    team = get_object_or_404(Team, id=team_id, entries__tournament=t)
    name = (request.POST.get('display_name') or '').strip()
    if name:
        TeamMembership.objects.create(team=team, display_name=name,
                                      jersey_number=request.POST.get('jersey_number', ''),
                                      is_approved=True)
        messages.success(request, f'Added {name} to {team.name}.')
    else:
        messages.error(request, 'Enter a name for the roster entry.')
    return redirect('participants_manage', slug=slug)


@approved_organizer_required
@require_POST
def team_members_bulk_import(request, slug, team_id):
    """Bulk-import a basketball roster from a CSV/.xlsx of Player Name + Jersey
    Number. Basketball-only; uses the same TeamMembership flow as manual add."""
    t = _owned(request, slug)
    if t.sport.slug != 'basketball':
        messages.error(request, 'Roster import is only available for basketball teams.')
        return redirect('participants_manage', slug=slug)

    team = get_object_or_404(Team, id=team_id, entries__tournament=t)
    upload = request.FILES.get('roster_file')
    if not upload:
        messages.error(request, 'Choose a CSV or .xlsx file to import.')
        return redirect('participants_manage', slug=slug)

    from .roster_import import parse_roster
    parsed = parse_roster(upload)

    # Skip roster names already on the team (case-insensitive), per the
    # account-less display_name model that has no DB uniqueness of its own.
    existing = {m.display_name.casefold()
                for m in team.memberships.filter(player__isnull=True)
                if m.display_name}

    added, dupes = 0, 0
    for row in parsed.members:
        if row['name'].casefold() in existing:
            dupes += 1
            continue
        TeamMembership.objects.create(team=team, display_name=row['name'],
                                      jersey_number=row['jersey'], is_approved=True)
        existing.add(row['name'].casefold())
        added += 1

    if added:
        messages.success(request, f'Imported {added} player(s) into {team.name}.')
    if dupes:
        messages.info(request, f'{dupes} player(s) already on {team.name} were skipped.')
    if parsed.skipped_blank:
        messages.info(request, f'{parsed.skipped_blank} empty row(s) were skipped.')
    for err in parsed.errors:
        messages.error(request, err)
    if not added and not parsed.errors and not dupes:
        messages.error(request, 'No players were imported — the file had no valid rows.')

    return redirect('participants_manage', slug=slug)


@approved_organizer_required
@require_POST
def member_decide(request, slug, membership_id, decision):
    t = _owned(request, slug)
    m = get_object_or_404(TeamMembership, id=membership_id, team__entries__tournament=t)
    if decision == 'approve':
        m.is_approved = True
        m.save(update_fields=['is_approved'])
        if m.player and m.player.user_id:
            Notification.push(m.player.user, f'You were added to {m.team.name} in {t.name}.',
                              url=t.get_absolute_url(), verb='team')
        messages.success(request, 'Join request approved.')
    elif decision == 'reject':
        if m.player and m.player.user_id:
            Notification.push(m.player.user,
                              f'Your request to join {m.team.name} in {t.name} was declined.',
                              url=t.get_absolute_url(), verb='team')
        m.delete()
        messages.info(request, 'Join request rejected.')
    else:
        messages.error(request, 'Unknown action.')
    return redirect('tournament_manage', slug=slug)


@approved_organizer_required
@require_POST
def individual_add(request, slug):
    t = _owned(request, slug)
    form = IndividualEntryForm(request.POST, tournament=t)
    if form.is_valid():
        reg = form.save(commit=False)
        reg.tournament = t
        reg.status = 'APPROVED'
        reg.save()
        messages.success(request, f'Registered {reg.name}.')
    else:
        messages.error(request, 'Could not add entrant — a display name is required.')
    return redirect('participants_manage', slug=slug)


@approved_organizer_required
@require_POST
def entry_decide(request, slug, kind, entry_id, decision):
    """Approve / reject / remove a team entry or an individual registration."""
    t = _owned(request, slug)
    model = TournamentTeamEntry if kind == 'team' else IndividualRegistration
    entry = get_object_or_404(model, tournament=t, id=entry_id)

    if decision == 'remove':
        if t.fixtures_generated:
            messages.warning(request, 'Fixtures already exist — regenerate them after this change.')
        entry.delete()
        messages.info(request, 'Entry removed.')
    elif decision in ('approve', 'reject'):
        entry.status = 'APPROVED' if decision == 'approve' else 'REJECTED'
        entry.save(update_fields=['status'])
        user = getattr(getattr(entry, 'player', None), 'user', None)
        if user:
            verb = 'accepted' if decision == 'approve' else 'declined'
            Notification.push(user, f'Your entry to {t.name} was {verb}.',
                              url=t.get_absolute_url(), verb='registration')
        messages.success(request, f'Entry {decision}d.')
    else:
        messages.error(request, 'Unknown action.')
    return redirect('participants_manage', slug=slug)


# ======================================================================
# Fixtures & scheduling
# ======================================================================
# Formats where one fixture is always exactly two entrants: fixtures for these
# are now arranged by the organiser (no more Seed/Random auto-generation).
# Group-session formats (time-trial, single-event, and the esports round-robin
# lobby, where one fixture holds every entrant at once) have no 1-vs-1 shape to
# pick, so they keep using the engine's bulk generator.
_MANUAL_PAIRWISE_FORMATS = {C.FORMAT_KNOCKOUT, C.FORMAT_ROUND_ROBIN}


def _manual_fixtures_supported(t):
    if t.format not in _MANUAL_PAIRWISE_FORMATS:
        return False
    if t.format == C.FORMAT_ROUND_ROBIN and t.sport.slug == 'mobile-esports':
        return False  # battle-royale lobby: every squad is in one fixture
    return True


def _manual_entrant_choices(t):
    """(key, label, payload) options for the organiser's fixture picker.

    `payload` matches the shape `engines._make_participant` expects, so a
    manually created fixture is stored exactly like an engine-generated one.
    """
    choices = []
    if t.is_team_based:
        for e in t.team_entries.filter(status='APPROVED').select_related('team'):
            choices.append((f'team:{e.team_id}', e.team.name,
                            {'team': e.team, 'player': None, 'label': e.team.name, 'stats': {}}))
    else:
        for r in t.registrations.filter(status='APPROVED').select_related('player__user'):
            stats = {}
            if r.bib_number:
                stats['bib'] = r.bib_number
            if r.player and r.player.rating:
                stats['rating'] = r.player.rating
            choices.append((f'reg:{r.id}', r.name,
                            {'team': None, 'player': r.player, 'label': r.name, 'stats': stats}))
    return choices


@approved_organizer_required
@require_POST
def fixtures_generate(request, slug):
    t = _owned(request, slug)
    if _manual_fixtures_supported(t):
        messages.error(request, 'Fixtures for this tournament are created by the organiser '
                                'below — use "Add fixture".')
        return redirect('fixtures_manage', slug=slug)
    if t.participant_count() < 2:
        messages.error(request, 'Add at least two participants before generating fixtures.')
        return redirect('participants_manage', slug=slug)
    from .engines import _entrants_for
    entrants = _entrants_for(t)
    count = t.engine.generate_fixtures(entrants)
    t.fixtures_generated = True
    if t.status == 'DRAFT':
        t.status = 'PUBLISHED'
    t.save(update_fields=['fixtures_generated', 'status', 'updated_at'])
    messages.success(request, f'Generated {count} fixtures. The tournament is now public.')
    return redirect('fixtures_manage', slug=slug)


@approved_organizer_required
@require_POST
def fixtures_generate_bracket(request, slug):
    """Auto-generate a full single-elimination knockout bracket.

    A second, additive way to populate fixtures for a knockout-format
    tournament, alongside (not instead of) the organiser-arranged "Add
    fixture" flow below — that manual flow is untouched by this view. This
    simply exposes the existing `BracketEngine`, which already seeds any
    number of teams, auto-generates byes up to the next power of two, and
    wires round-to-round advancement, exactly as it does for every other
    knockout sport.
    """
    t = _owned(request, slug)
    if t.format != C.FORMAT_KNOCKOUT:
        messages.error(request, 'The knockout bracket generator is only available for '
                                'knockout-format tournaments.')
        return redirect('fixtures_manage', slug=slug)
    if t.participant_count() < 2:
        messages.error(request, 'Add at least two participants before generating a bracket.')
        return redirect('participants_manage', slug=slug)
    from .engines import _entrants_for
    entrants = _entrants_for(t)
    count = t.engine.generate_fixtures(entrants)
    t.fixtures_generated = True
    if t.status == 'DRAFT':
        t.status = 'PUBLISHED'
    t.save(update_fields=['fixtures_generated', 'status', 'updated_at'])
    messages.success(request, f'Generated a {len(entrants)}-team knockout bracket ({count} fixtures).')
    return redirect('fixtures_manage', slug=slug)


@approved_organizer_required
@require_POST
def fixture_add_manual(request, slug):
    """Organiser-arranged fixture creation: pick two entrants, create one
    fixture. Additive — existing fixtures are never cleared — so the
    organiser builds the schedule up one match at a time."""
    t = _owned(request, slug)
    if not _manual_fixtures_supported(t):
        messages.error(request, 'Manual fixture creation is not available for this tournament format.')
        return redirect('fixtures_manage', slug=slug)

    key_a = (request.POST.get('entrant_a') or '').strip()
    key_b = (request.POST.get('entrant_b') or '').strip()
    if not key_a or not key_b:
        messages.error(request, 'Select both teams before creating a fixture.')
        return redirect('fixtures_manage', slug=slug)
    if key_a == key_b:
        messages.error(request, 'A team cannot play against itself.')
        return redirect('fixtures_manage', slug=slug)

    choices = {key: payload for key, _, payload in _manual_entrant_choices(t)}
    a, b = choices.get(key_a), choices.get(key_b)
    if a is None or b is None:
        messages.error(request, 'Select two valid participating teams.')
        return redirect('fixtures_manage', slug=slug)

    round_raw = (request.POST.get('round_no') or '').strip()
    round_no = int(round_raw) if round_raw.isdigit() and int(round_raw) > 0 else 1
    round_name = (request.POST.get('round_name') or '').strip() or f'Round {round_no}'

    from .engines import _make_participant
    fx = Fixture.objects.create(tournament=t, round_no=round_no, sequence=t.fixtures.count(),
                                round_name=round_name, created_by=request.user)
    _make_participant(fx, a, 0)
    _make_participant(fx, b, 1)

    t.fixtures_generated = True
    if t.status == 'DRAFT':
        t.status = 'PUBLISHED'
    t.save(update_fields=['fixtures_generated', 'status', 'updated_at'])
    messages.success(request, f'Fixture created: {a["label"]} vs {b["label"]}.')
    return redirect('fixtures_manage', slug=slug)


def _knockout_bracket_context(fixtures):
    """Group a knockout tournament's fixtures into bracket_rounds + champion —
    same shape `public/_bracket.html` already renders on the public detail
    page, reused here so the organiser sees the same bracket while managing
    fixtures. Works for fixtures from either the bracket generator or the
    manual "Add fixture" flow, since both just need round_no/round_name.
    """
    rounds = {}
    for fx in sorted(fixtures, key=lambda f: (f.round_no, f.sequence)):
        rounds.setdefault(fx.round_no, {'name': fx.round_name, 'fixtures': []})
        rounds[fx.round_no]['fixtures'].append(fx)
    bracket_rounds = [rounds[k] for k in sorted(rounds)]
    champion = None
    if rounds:
        final = rounds[max(rounds)]['fixtures']
        if len(final) == 1 and final[0].status == 'COMPLETED':
            champion = next((p for p in final[0].participants.all() if p.is_winner), None)
    return bracket_rounds, champion


@approved_organizer_required
def fixtures_manage(request, slug):
    t = _owned(request, slug)
    fixtures = t.fixtures.filter(is_removed=False).prefetch_related(
        Prefetch('participants',
                 queryset=FixtureParticipant.objects.select_related('team', 'player__user')))
    manual_mode = _manual_fixtures_supported(t)
    is_knockout = t.format == C.FORMAT_KNOCKOUT
    ctx = {
        'tournament': t, 'fixtures': fixtures,
        'manual_mode': manual_mode,
        'entrant_choices': _manual_entrant_choices(t) if manual_mode else [],
        'is_knockout': is_knockout,
    }
    if is_knockout:
        bracket_rounds, champion = _knockout_bracket_context(list(fixtures))
        ctx['bracket_rounds'] = bracket_rounds
        ctx['champion'] = champion
    return render(request, 'organizer/fixtures.html', ctx)


@approved_organizer_required
@require_POST
def fixture_delete(request, slug, fixture_id):
    """Remove a single fixture from the Fixtures & Scoring list.

    Soft-delete via the existing `Fixture.is_removed` flag — the same
    mechanism every fixture query in the app already filters on (the fixture
    list, live-score ticker, and standings), so removal is immediate and
    complete everywhere without touching any other fixture, team, player, or
    the tournament itself.
    """
    t = _owned(request, slug)
    fixture = get_object_or_404(Fixture, id=fixture_id, tournament=t, is_removed=False)
    fixture.is_removed = True
    fixture.save(update_fields=['is_removed', 'updated_at'])
    t.engine.compute_standings()  # no-op for formats without a table; refreshes round-robin
    messages.success(request, 'Fixture deleted.')
    return redirect('fixtures_manage', slug=slug)


@approved_organizer_required
@require_POST
def fixtures_clear_all(request, slug):
    """Bulk version of fixture_delete: remove every already-generated fixture
    for this tournament in one go, regardless of how they were created
    (engine bulk-generate, the knockout bracket generator, or added one at a
    time by the organiser). Same soft-delete mechanism as deleting a single
    fixture, just applied to all of them, so every other view/query that
    already filters on `is_removed` picks the change up automatically.
    """
    t = _owned(request, slug)
    count = t.fixtures.filter(is_removed=False).update(is_removed=True, updated_at=timezone.now())
    if count:
        t.fixtures_generated = False
        t.save(update_fields=['fixtures_generated', 'updated_at'])
        t.engine.compute_standings()  # no-op for formats without a table; clears round-robin
        messages.success(request, f'Removed {count} fixture(s). Generate or add fixtures to start over.')
    else:
        messages.info(request, 'There are no fixtures to remove.')
    return redirect('fixtures_manage', slug=slug)


@approved_organizer_required
def fixture_schedule(request, slug, fixture_id):
    """Set kick-off time and court/lane for one fixture."""
    t = _owned(request, slug)
    fixture = get_object_or_404(Fixture, id=fixture_id, tournament=t)
    form = FixtureScheduleForm(request.POST or None, instance=fixture)
    if request.method == 'POST' and form.is_valid():
        form.save()
        for p in fixture.participants.select_related('player__user'):
            if p.player and p.player.user_id and fixture.scheduled_time:
                Notification.push(
                    p.player.user,
                    f'{t.name}: your match is scheduled for '
                    f'{timezone.localtime(fixture.scheduled_time):%d %b, %H:%M}.',
                    url=fixture.get_absolute_url(), verb='schedule')
        messages.success(request, 'Schedule updated.')
        return redirect('fixtures_manage', slug=slug)
    return render(request, 'organizer/schedule.html',
                  {'tournament': t, 'fixture': fixture, 'form': form})


# ======================================================================
# Live scoring
# ======================================================================
_BASKETBALL_POINT_VALUES = {'1', '2', '3'}
_SHOT_CLOCK_DEFAULT_SECONDS = 24


def _basketball_scoring_context(fixture):
    """Player choices per team-side and the individual scoring+fouls table —
    all derived from existing data (TeamMembership rosters, ScoreEvent rows),
    so nothing here needs a new source of truth beyond what's already
    persisted.
    """
    participants = list(fixture.participants.select_related('team').all())
    player_choices = {
        p.id: (list(p.team.memberships.filter(is_approved=True).order_by('jersey_number'))
               if p.team_id else [])
        for p in participants
    }

    totals = {}

    def row_for(mid, snap, participant):
        return totals.setdefault(mid, {
            'name': snap.get('player_name', ''), 'jersey_number': snap.get('jersey_number', ''),
            'team_name': participant.name if participant else '',
            'pt1': 0, 'pt2': 0, 'pt3': 0, 'total': 0, 'fouls': 0,
        })

    for ev in fixture.events.filter(event_type='score').select_related('participant__team'):
        snap = ev.score_snapshot or {}
        mid = snap.get('membership_id')
        pts = int(snap.get('points') or 0)
        if not mid or pts not in (1, 2, 3):
            continue
        row = row_for(mid, snap, ev.participant)
        row[f'pt{pts}'] += 1
        row['total'] += pts

    for ev in fixture.events.filter(event_type='foul').select_related('participant__team'):
        snap = ev.score_snapshot or {}
        mid = snap.get('membership_id')
        if not mid:
            continue
        row = row_for(mid, snap, ev.participant)
        row['fouls'] += 1

    individual_rows = sorted(totals.values(), key=lambda r: (-r['total'], -r['fouls'], r['name'].lower()))
    return player_choices, individual_rows


@approved_organizer_required
def score_fixture(request, slug, fixture_id):
    t = _owned(request, slug)
    fixture = get_object_or_404(Fixture, id=fixture_id, tournament=t)
    participants = list(fixture.ordered_participants())
    is_basketball = t.sport.slug == 'basketball'

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'start':
            fixture.status = 'LIVE'
            update_fields = ['status', 'updated_at']
            if not fixture.live_started_at:
                now = timezone.now()
                fixture.live_started_at = now
                update_fields.append('live_started_at')
                if is_basketball:
                    # The quarter clock starts paused (frozen at the full
                    # quarter length) until the organizer explicitly taps
                    # Play — it should never auto-tick just because the
                    # match went LIVE.
                    fixture.clock_paused_at = now
                    update_fields.append('clock_paused_at')
            fixture.save(update_fields=update_fields)
            sync_tournament_status(t)
            messages.info(request, 'Match marked LIVE.')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'youtube':
            fixture.youtube_url = (request.POST.get('youtube_url') or '').strip()
            fixture.save(update_fields=['youtube_url', 'updated_at'])
            messages.success(request, 'Stream link updated.')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'commentary':
            text = (request.POST.get('commentary') or '').strip()
            if text:
                ScoreEvent.objects.create(fixture=fixture, description=text[:280],
                                          created_by=request.user, event_type='note')
                messages.success(request, 'Commentary posted.')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'extra_time':
            if not is_basketball:
                messages.error(request, 'Extra time is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if fixture.status != 'LIVE':
                messages.error(request, 'Start the match before adding extra time.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            raw = (request.POST.get('extra_minutes') or '').strip()
            minutes = int(raw) if raw.isdigit() and int(raw) > 0 else 0
            if minutes:
                fixture.extra_time_seconds += minutes * 60
                fixture.save(update_fields=['extra_time_seconds', 'updated_at'])
                messages.success(request, f'Added {minutes} minute(s) of extra time.')
            else:
                messages.error(request, 'Enter a positive number of minutes.')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'set_period':
            if not is_basketball:
                messages.error(request, 'Quarter navigation is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            direction = request.POST.get('direction')
            old_period = fixture.current_period or 1
            period = old_period
            if direction == 'next':
                period += 1
            elif direction == 'prev':
                period = max(1, period - 1)
            fixture.current_period = period
            update_fields = ['current_period', 'updated_at']
            if direction == 'next' and period != old_period and fixture.status == 'LIVE':
                # Advancing to a genuinely new quarter restarts the clocks and
                # TEAM fouls (bonus/penalty resets each quarter). Going back a
                # quarter (organizer correcting a misclick) does NOT reset
                # anything — the clock/fouls only ever restart when moving
                # forward into a quarter not yet played. Score is a
                # whole-game running total and each player's PERSONAL foul
                # count is cumulative for the whole game (foul-out tracking),
                # so neither is touched here.
                # Paused, not running — same reasoning as reset_quarter_clock:
                # a fresh quarter never auto-ticks until the organizer taps
                # Play.
                now = timezone.now()
                fixture.live_started_at = now
                fixture.extra_time_seconds = 0
                fixture.clock_paused_at = now
                fixture.shot_clock_seconds_remaining = _SHOT_CLOCK_DEFAULT_SECONDS
                fixture.shot_clock_running_since = None
                update_fields += ['live_started_at', 'extra_time_seconds', 'clock_paused_at',
                                  'shot_clock_seconds_remaining', 'shot_clock_running_since']
                fixture.participants.update(fouls=0)
            fixture.save(update_fields=update_fields)
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'foul':
            if not is_basketball:
                messages.error(request, 'Foul tracking is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            participant = get_object_or_404(FixtureParticipant, id=request.POST.get('participant_id'),
                                            fixture=fixture)
            delta = request.POST.get('delta')
            membership_id = request.POST.get('membership_id')
            with transaction.atomic():
                if delta == 'inc':
                    participant.fouls = (participant.fouls or 0) + 1
                    participant.save(update_fields=['fouls'])
                    # A foul record is always kept (team-level "no player"
                    # fouls included) so the "-" button below can pop the
                    # true most-recent foul off the stack, not just the most
                    # recent *attributed* one.
                    membership = None
                    if membership_id:
                        membership = get_object_or_404(TeamMembership, id=membership_id,
                                                       team_id=participant.team_id)
                    ScoreEvent.objects.create(
                        fixture=fixture, participant=participant, event_type='foul',
                        description=(f'Foul — {membership.name} (#{membership.jersey_number or "-"})'
                                     if membership else f'Foul — {participant.name} (team)'),
                        score_snapshot={'membership_id': membership.id if membership else None,
                                        'player_name': membership.name if membership else '',
                                        'jersey_number': membership.jersey_number if membership else '',
                                        'team_participant_id': participant.id},
                        created_by=request.user)
                elif delta == 'dec' and (participant.fouls or 0) > 0:
                    participant.fouls -= 1
                    participant.save(update_fields=['fouls'])
                    # LIFO: drop the most recently recorded foul for this
                    # team, whether or not it was attributed to a player.
                    last_foul = fixture.events.filter(
                        participant=participant, event_type='foul').order_by('-id').first()
                    if last_foul:
                        last_foul.delete()
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'set_quarter_length':
            if not is_basketball:
                messages.error(request, 'Quarter length is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            raw = (request.POST.get('minutes') or '').strip()
            minutes = int(raw) if raw.isdigit() and 1 <= int(raw) <= 60 else 0
            if minutes:
                fixture.quarter_length_seconds = minutes * 60
                fixture.save(update_fields=['quarter_length_seconds', 'updated_at'])
                messages.success(request, f'Quarter length set to {minutes} min.')
            else:
                messages.error(request, 'Enter a quarter length between 1 and 60 minutes.')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'shot_clock_start':
            if not is_basketball:
                messages.error(request, 'The shot clock is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if (fixture.status == 'LIVE' and not fixture.shot_clock_running_since
                    and fixture.shot_clock_seconds_remaining > 0):
                fixture.shot_clock_running_since = timezone.now()
                fixture.save(update_fields=['shot_clock_running_since', 'updated_at'])
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'shot_clock_pause':
            if not is_basketball:
                messages.error(request, 'The shot clock is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if fixture.shot_clock_running_since:
                elapsed = (timezone.now() - fixture.shot_clock_running_since).total_seconds()
                fixture.shot_clock_seconds_remaining = max(
                    0, int(fixture.shot_clock_seconds_remaining - elapsed))
                fixture.shot_clock_running_since = None
                fixture.save(update_fields=['shot_clock_seconds_remaining', 'shot_clock_running_since',
                                            'updated_at'])
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'shot_clock_reset':
            if not is_basketball:
                messages.error(request, 'The shot clock is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            fixture.shot_clock_seconds_remaining = _SHOT_CLOCK_DEFAULT_SECONDS
            fixture.shot_clock_running_since = None
            fixture.save(update_fields=['shot_clock_seconds_remaining', 'shot_clock_running_since',
                                        'updated_at'])
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'pause_clock':
            if not is_basketball:
                messages.error(request, 'The match clock is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if fixture.status == 'LIVE' and fixture.live_started_at and not fixture.clock_paused_at:
                fixture.clock_paused_at = timezone.now()
                fixture.save(update_fields=['clock_paused_at', 'updated_at'])
                messages.info(request, 'Match clock paused.')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'resume_clock':
            if not is_basketball:
                messages.error(request, 'The match clock is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if fixture.clock_paused_at and fixture.live_started_at:
                fixture.live_started_at += timezone.now() - fixture.clock_paused_at
                fixture.clock_paused_at = None
                fixture.save(update_fields=['live_started_at', 'clock_paused_at', 'updated_at'])
                messages.info(request, 'Match clock resumed.')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'reset_quarter_clock':
            if not is_basketball:
                messages.error(request, 'The match clock is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if fixture.status == 'LIVE':
                # Resets the clock to the full quarter length, paused — the
                # organizer taps Play to actually start it counting down,
                # same as a fresh match start or a new quarter.
                now = timezone.now()
                fixture.live_started_at = now
                fixture.extra_time_seconds = 0
                fixture.clock_paused_at = now
                fixture.save(update_fields=['live_started_at', 'extra_time_seconds', 'clock_paused_at',
                                            'updated_at'])
                messages.success(request, 'Quarter timer reset.')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'adjust_score':
            if not is_basketball:
                messages.error(request, 'Score adjustment is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if fixture.status != 'LIVE':
                messages.error(request, 'Start the match before adjusting the score.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            participant = get_object_or_404(FixtureParticipant, id=request.POST.get('participant_id'),
                                            fixture=fixture)
            participant.score = max(0, (participant.score or 0) - 1)
            participant.save(update_fields=['score'])
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'score_point':
            if not is_basketball:
                messages.error(request, 'Point scoring is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if fixture.status != 'LIVE':
                messages.error(request, 'Start the match before recording points.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            points_raw = request.POST.get('points')
            membership_id = request.POST.get('membership_id')
            if points_raw not in _BASKETBALL_POINT_VALUES:
                messages.error(request, 'Choose +1, +2, or +3.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if not membership_id:
                messages.error(request, 'Select the scoring player before recording a point.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            participant = get_object_or_404(FixtureParticipant, id=request.POST.get('participant_id'),
                                            fixture=fixture)
            # Scoped to `participant`'s own team — a player from the other side
            # simply cannot resolve here, whatever the client sent.
            membership = get_object_or_404(TeamMembership, id=membership_id, team_id=participant.team_id)
            points = int(points_raw)
            with transaction.atomic():
                participant.score = (participant.score or 0) + points
                participant.save(update_fields=['score'])
                ScoreEvent.objects.create(
                    fixture=fixture, participant=participant, event_type='score',
                    description=f'{participant.name} +{points} — {membership.name} '
                                f'(#{membership.jersey_number or "-"})',
                    score_snapshot={'membership_id': membership.id, 'player_name': membership.name,
                                    'jersey_number': membership.jersey_number, 'points': points,
                                    'team_participant_id': participant.id},
                    created_by=request.user)
            messages.success(request, f'+{points} — {membership.name} (#{membership.jersey_number or "-"}).')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        if action == 'undo_last_score':
            if not is_basketball:
                messages.error(request, 'Undo is only available for basketball matches.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            if fixture.status != 'LIVE':
                messages.error(request, 'Start the match before undoing an action.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            # Most-recent-first ordering by primary key (not created_at,
            # which can tie within the same second) gives a reliable LIFO
            # undo stack over the score events already persisted per point.
            last_event = fixture.events.filter(event_type='score').order_by('-id').first()
            if not last_event:
                messages.error(request, 'Nothing to undo.')
                return redirect('score_fixture', slug=slug, fixture_id=fixture_id)
            with transaction.atomic():
                snap = last_event.score_snapshot or {}
                points = int(snap.get('points') or 0)
                participant = last_event.participant
                if participant is not None:
                    participant.score = max(0, (participant.score or 0) - points)
                    participant.save(update_fields=['score'])
                player_name = snap.get('player_name') or 'that player'
                last_event.delete()
            messages.success(request, f'Undid +{points} — {player_name}.')
            return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

        # --- parse a score submission per format ---
        data = {'finalize': action == 'finalize'}
        fmt = t.format
        is_lobby = (fmt == C.FORMAT_ROUND_ROBIN and t.sport.slug == 'mobile-esports')
        for p in participants:
            key = str(p.id)
            if fmt in (C.FORMAT_TIME_TRIAL, C.FORMAT_SINGLE_EVENT):
                data[key] = {'time_ms': parse_time_to_ms(request.POST.get(f'time_{p.id}', '')),
                             'result_state': request.POST.get(f'state_{p.id}', 'OK')}
            elif is_lobby:
                data[key] = {'kills': request.POST.get(f'kills_{p.id}') or 0,
                             'placement': request.POST.get(f'place_{p.id}') or ''}
            else:
                raw = request.POST.get(f'score_{p.id}', '')
                data[key] = {'score': raw if raw != '' else None}
        apply_result(fixture, data, actor=request.user)
        messages.success(request, 'Result saved.' + (' Match finalized.' if data['finalize'] else ''))
        return redirect('score_fixture', slug=slug, fixture_id=fixture_id)

    ctx = {
        'tournament': t, 'fixture': fixture, 'participants': participants,
        'is_lobby': (t.format == C.FORMAT_ROUND_ROBIN and t.sport.slug == 'mobile-esports'),
        'is_time': t.format in (C.FORMAT_TIME_TRIAL, C.FORMAT_SINGLE_EVENT),
        'is_basketball': is_basketball,
        'events': fixture.events.exclude(event_type='score')[:30],
        'format_ms': format_ms,
    }
    if is_basketball:
        player_choices, individual_rows = _basketball_scoring_context(fixture)
        ctx['player_choices'] = player_choices
        ctx['individual_rows'] = individual_rows
        ctx['can_undo_score'] = fixture.events.filter(event_type='score').exists()
    return render(request, 'organizer/score.html', ctx)


# ======================================================================
# Highlights
# ======================================================================
@approved_organizer_required
def highlight_manage(request, slug, fixture_id):
    t = _owned(request, slug)
    fixture = get_object_or_404(Fixture, id=fixture_id, tournament=t)
    instance = fixture.highlights.first()
    form = HighlightForm(request.POST or None, request.FILES or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        h = form.save(commit=False)
        h.tournament = t
        h.fixture = fixture
        h.created_by = request.user
        if request.POST.get('publish'):
            h.published_at = h.published_at or timezone.now()
        h.save()
        messages.success(request, 'Highlights saved.')
        return redirect('match_detail', slug=t.slug, pk=fixture.id)
    return render(request, 'organizer/highlight.html',
                  {'tournament': t, 'fixture': fixture, 'form': form})


# ======================================================================
# Player participation
# ======================================================================
@player_required
def tournament_join(request, slug):
    t = get_object_or_404(Tournament.objects.public(), slug=slug)
    profile, _ = PlayerProfile.objects.get_or_create(user=request.user)

    if t.status in ('COMPLETED', 'CANCELLED'):
        messages.error(request, 'This tournament is no longer accepting entries.')
        return redirect('tournament_detail', slug=slug)
    if t.registration_deadline and timezone.now() > t.registration_deadline:
        messages.error(request, 'Registration has closed.')
        return redirect('tournament_detail', slug=slug)

    if request.method == 'POST':
        if t.is_team_based:
            team = get_object_or_404(Team, id=request.POST.get('team_id'), entries__tournament=t)
            if TeamMembership.objects.filter(team=team, player=profile).exists():
                messages.info(request, 'You have already requested to join this team.')
            else:
                TeamMembership.objects.create(team=team, player=profile, is_approved=False)
                Notification.push(
                    t.organizer.user,
                    f'{profile.user.display_name} requested to join {team.name}.',
                    url=f'/organizer/t/{t.slug}/', verb='join')
                messages.success(request, 'Join request sent to the organizer.')
        else:
            if t.max_participants and t.participant_count() >= t.max_participants:
                messages.error(request, 'This tournament is full.')
                return redirect('tournament_detail', slug=slug)
            _, created = IndividualRegistration.objects.get_or_create(
                tournament=t, player=profile,
                defaults={'display_name': profile.user.display_name, 'status': 'APPROVED'})
            if created:
                Notification.push(t.organizer.user,
                                  f'{profile.user.display_name} registered for {t.name}.',
                                  url=f'/organizer/t/{t.slug}/participants', verb='registration')
            messages.success(request, 'You are registered!' if created else 'Already registered.')
        return redirect('tournament_detail', slug=slug)

    teams = t.team_entries.select_related('team').filter(status='APPROVED') if t.is_team_based else None
    return render(request, 'players/join.html', {'tournament': t, 'teams': teams})


# ======================================================================
# Following a tournament
# ======================================================================
@player_required
@require_POST
def tournament_follow(request, slug):
    """Follow / unfollow. Followers are the audience for result notifications —
    the one channel that reaches someone who is neither playing nor organizing."""
    t = get_object_or_404(Tournament.objects.public(), slug=slug)
    existing = Follow.objects.filter(user=request.user, tournament=t).first()
    if existing:
        existing.delete()
        messages.info(request, f'You are no longer following {t.name}.')
    else:
        Follow.objects.create(user=request.user, tournament=t)
        messages.success(request, f'Following {t.name} — we will tell you when results land.')
    return redirect(request.META.get('HTTP_REFERER') or t.get_absolute_url())


@player_required
def my_following(request):
    tournaments = Tournament.objects.filter(
        followers__user=request.user).select_related('sport', 'organizer__user').distinct()
    return render(request, 'players/following.html', {'tournaments': tournaments})


# ======================================================================
# Live-score JSON API (public, polled)
# ======================================================================
def fixture_live_json(request, fixture_id):
    fixture = get_object_or_404(
        Fixture.objects.select_related('tournament__sport'), id=fixture_id, is_removed=False)
    # Never leak an unpublished or moderated-away tournament through the API.
    t = fixture.tournament
    if t.status == 'DRAFT' or t.is_removed:
        raise PermissionDenied()

    parts = [{
        'id': p.id, 'name': p.name, 'initials': p.initials,
        'score': float(p.score) if p.score is not None else None,
        'rank': p.rank, 'is_winner': p.is_winner,
        'state': p.result_state,
        'time': p.time_display or None,
        'bib': p.bib or None,
        'pace': p.pace_display or None,
        'kills': p.kills,
    } for p in fixture.ordered_participants()]

    clock = None
    if t.sport.slug == 'basketball':
        # Same fields + the same server-computed paused_quarter_remaining_seconds
        # the organizer scoring page already renders — the public match page's
        # quarter-clock element (static/js/match-clock.js) reads these exact
        # attributes, so this just keeps that one clock in sync, not a second one.
        clock = {
            'started_at': fixture.live_started_at.isoformat() if fixture.live_started_at else None,
            'extra_seconds': fixture.extra_time_seconds,
            'quarter_length_seconds': fixture.quarter_length_seconds,
            'paused': bool(fixture.clock_paused_at),
            'paused_remaining_seconds': fixture.paused_quarter_remaining_seconds,
        }

    return JsonResponse({
        'status': fixture.status,
        'round': fixture.round_name,
        'participants': parts,
        # Drives the win-probability bar on the live scoreboard.
        'win_probability': fixture.win_probability,
        'clock': clock,
        'events': [{'text': e.description, 'at': e.created_at.strftime('%H:%M')}
                   for e in fixture.events.all()[:15]],
        'updated': timezone.now().isoformat(),
    })
