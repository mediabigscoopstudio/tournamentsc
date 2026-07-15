from .models import Fixture


def ticker(request):
    """A few live fixtures for the site-wide live-score ticker bar."""
    fixtures = (Fixture.objects.filter(status='LIVE', is_removed=False)
                .exclude(tournament__status__in=['DRAFT', 'CANCELLED'])
                .filter(tournament__is_removed=False)
                .select_related('tournament__sport')
                .prefetch_related('participants__team', 'participants__player__user')[:8])
    return {'ticker_fixtures': list(fixtures)}
