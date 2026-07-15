from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import approved_organizer_required, player_required
from accounts.models import Follow, Notification, PlayerProfile
from . import constants as C
from .engines import random_seed_entrants
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
@approved_organizer_required
@require_POST
def fixtures_generate(request, slug):
    t = _owned(request, slug)
    if t.participant_count() < 2:
        messages.error(request, 'Add at least two participants before generating fixtures.')
        return redirect('participants_manage', slug=slug)
    from .engines import _entrants_for
    entrants = _entrants_for(t)
    if request.POST.get('seeding') == 'random':
        entrants = random_seed_entrants(entrants)
    count = t.engine.generate_fixtures(entrants)
    t.fixtures_generated = True
    if t.status == 'DRAFT':
        t.status = 'PUBLISHED'
    t.save(update_fields=['fixtures_generated', 'status', 'updated_at'])
    messages.success(request, f'Generated {count} fixtures. The tournament is now public.')
    return redirect('fixtures_manage', slug=slug)


@approved_organizer_required
def fixtures_manage(request, slug):
    t = _owned(request, slug)
    fixtures = t.fixtures.filter(is_removed=False).prefetch_related(
        Prefetch('participants',
                 queryset=FixtureParticipant.objects.select_related('team', 'player__user')))
    return render(request, 'organizer/fixtures.html', {'tournament': t, 'fixtures': fixtures})


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
@approved_organizer_required
def score_fixture(request, slug, fixture_id):
    t = _owned(request, slug)
    fixture = get_object_or_404(Fixture, id=fixture_id, tournament=t)
    participants = list(fixture.ordered_participants())

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'start':
            fixture.status = 'LIVE'
            fixture.save(update_fields=['status', 'updated_at'])
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

    return render(request, 'organizer/score.html', {
        'tournament': t, 'fixture': fixture, 'participants': participants,
        'is_lobby': (t.format == C.FORMAT_ROUND_ROBIN and t.sport.slug == 'mobile-esports'),
        'is_time': t.format in (C.FORMAT_TIME_TRIAL, C.FORMAT_SINGLE_EVENT),
        'events': fixture.events.all()[:30],
        'format_ms': format_ms,
    })


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
        Fixture.objects.select_related('tournament'), id=fixture_id, is_removed=False)
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

    return JsonResponse({
        'status': fixture.status,
        'round': fixture.round_name,
        'participants': parts,
        # Drives the win-probability bar on the live scoreboard.
        'win_probability': fixture.win_probability,
        'events': [{'text': e.description, 'at': e.created_at.strftime('%H:%M')}
                   for e in fixture.events.all()[:15]],
        'updated': timezone.now().isoformat(),
    })
