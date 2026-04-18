def auth_context(request):
    user = request.user
    if user.is_authenticated:
        return {
            'discord_user': {
                'username': user.discord_username or user.username,
                'avatar': user.discord_avatar,
                'id': str(user.discord_id or ''),
            }
        }
    return {'discord_user': None}
