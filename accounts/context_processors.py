def notifications(request):
    """Unread notification count + recent list for the header bell."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated):
        return {}
    qs = user.notifications.all()
    return {
        'nav_unread_count': qs.filter(is_read=False).count(),
        'nav_recent_notifications': list(qs[:6]),
    }
