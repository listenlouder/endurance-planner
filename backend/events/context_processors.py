def _login_next(request):
    """
    Where to send the user after a Discord login started from this page.

    Returns the current path so logging in from any page brings the user back
    to it. Paths under /accounts/ are the auth machinery itself — bouncing back
    into a callback or error URL would restart or re-fail the flow — so those
    fall back to the home page.
    """
    path = request.get_full_path()
    if path.startswith('/accounts/'):
        return '/'
    return path


def auth_context(request):
    user = request.user
    context = {'login_next': _login_next(request)}

    if user.is_authenticated:
        context['discord_user'] = {
            'username': user.discord_username or user.username,
            'avatar': user.discord_avatar,
            'id': str(user.discord_id or ''),
        }
    else:
        context['discord_user'] = None

    return context
