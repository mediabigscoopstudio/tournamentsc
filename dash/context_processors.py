"""Make admin-managed site configuration available to every template."""
from django.utils import timezone

from .models import Announcement, SiteSetting


def site(request):
    conf = SiteSetting.load()
    banners = []
    # Announcement banners are a public-site feature; the admin console has its
    # own messaging and should not be interrupted by them.
    if not request.path.startswith('/dashboard'):
        now = timezone.now()
        candidates = Announcement.objects.filter(
            status=Announcement.STATUS_PUBLISHED, is_archived=False, show_on_site=True,
            published_at__isnull=False, published_at__lte=now)
        banners = [a for a in candidates if a.is_current][:3]
    return {'site_conf': conf, 'site_announcements': banners}
