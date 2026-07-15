from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect


class SuspendedUserMiddleware:
    """Suspended accounts are logged out immediately on their next request.
    Their public content is hidden by querysets elsewhere.

    The bounce lands on the login page for the area the user was in, so a
    suspended admin is not dumped onto the public player sign-in.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user and user.is_authenticated and getattr(user, 'is_suspended', False):
            in_admin = request.path.startswith('/dashboard')
            logout(request)
            messages.error(request, 'Your account has been suspended. Contact the TournamentSC team.')
            return redirect('dash_login' if in_admin else 'login')
        return self.get_response(request)
