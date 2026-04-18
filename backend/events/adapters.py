from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class DiscordAccountAdapter(DefaultSocialAccountAdapter):

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        self._update_discord_fields(user, sociallogin)
        return user

    def pre_social_login(self, request, sociallogin):
        super().pre_social_login(request, sociallogin)
        if sociallogin.is_existing:
            self._update_discord_fields(sociallogin.user, sociallogin)

    def _update_discord_fields(self, user, sociallogin):
        extra = sociallogin.account.extra_data

        discord_id = extra.get('id', '')
        display_name = extra.get('global_name') or extra.get('username', '')
        avatar_hash = extra.get('avatar') or ''

        if discord_id and avatar_hash and avatar_hash.isalnum():
            avatar_url = (
                f"https://cdn.discordapp.com/avatars/"
                f"{discord_id}/{avatar_hash}.png?size=128"
            )
        else:
            avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"

        new_username = discord_id if discord_id else user.username

        dirty = (
            user.discord_id != discord_id
            or user.discord_username != display_name
            or user.discord_avatar != avatar_url
            or user.username != new_username
        )
        if not dirty:
            return

        user.discord_id = discord_id
        user.discord_username = display_name
        user.discord_avatar = avatar_url
        user.username = new_username

        user.save(update_fields=[
            'discord_id',
            'discord_username',
            'discord_avatar',
            'username',
        ])
