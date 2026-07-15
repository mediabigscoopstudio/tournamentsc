"""Branded error pages.

A 403 inside the admin console must not leak the public site's navigation, and a
403 on the public site must not hint that an admin console exists. Each handler
picks the right shell for the area the request was in.
"""
from django.shortcuts import render


def _area(request):
    return 'dash' if request.path.startswith('/dashboard') else 'public'


def permission_denied(request, exception=None):
    return render(request, 'errors/403.html', {'area': _area(request)}, status=403)


def page_not_found(request, exception=None):
    return render(request, 'errors/404.html', {'area': _area(request)}, status=404)


def server_error(request):
    return render(request, 'errors/500.html', {'area': _area(request)}, status=500)
