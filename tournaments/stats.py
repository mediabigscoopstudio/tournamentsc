"""Read-only aggregation over existing fixture/result data (PLAYER-03/04)."""
from django.db.models import Q

from .models import Fixture, Tournament


def _player_tournaments(profile):
    """Every tournament a player is in — via team roster or individual entry."""
    team_ids = list(profile.team_memberships.values_list('team_id', flat=True))
    q = Q(registrations__player=profile)
    if team_ids:
        q |= Q(team_entries__team_id__in=team_ids)
    return Tournament.objects.filter(q).distinct()


def player_schedule(profile):
    """Upcoming fixtures across all the player's tournaments, soonest first."""
    tournaments = _player_tournaments(profile)
    team_ids = list(profile.team_memberships.values_list('team_id', flat=True))
    fixtures = Fixture.objects.filter(
        tournament__in=tournaments, is_removed=False,
        status__in=['SCHEDULED', 'LIVE', 'POSTPONED'],
    ).filter(Q(participants__player=profile) | Q(participants__team_id__in=team_ids)) \
     .select_related('tournament', 'tournament__sport').distinct() \
     .order_by('scheduled_time', 'round_no', 'sequence')
    return fixtures


def player_results(profile):
    team_ids = list(profile.team_memberships.values_list('team_id', flat=True))
    tournaments = _player_tournaments(profile)
    fixtures = Fixture.objects.filter(
        tournament__in=tournaments, is_removed=False, status='COMPLETED',
    ).filter(Q(participants__player=profile) | Q(participants__team_id__in=team_ids)) \
     .select_related('tournament', 'tournament__sport').distinct() \
     .order_by('-updated_at')
    return fixtures


def player_history(profile):
    """Aggregate: tournaments played + a per-tournament summary row."""
    tournaments = _player_tournaments(profile).select_related('sport')
    return {
        'count': tournaments.count(),
        'tournaments': list(tournaments[:50]),
    }
