from django.core.management.base import BaseCommand
from django.contrib.sites.models import Site
from allauth.socialaccount.models import SocialApp
from django.conf import settings


class Command(BaseCommand):
    help = (
        'Configure Discord OAuth provider for allauth. '
        'Run once after initial deploy or when credentials change.'
    )

    def handle(self, *args, **options):
        if not settings.DISCORD_CLIENT_ID:
            self.stdout.write(
                self.style.WARNING(
                    'DISCORD_CLIENT_ID not set — skipping OAuth setup.'
                )
            )
            return

        # allauth resolves the SocialApp by SITE_ID rather than by matching
        # this domain against the request host, and it builds the Discord
        # callback from the request — so this value does not steer the OAuth
        # round-trip. It is still what absolute URLs built without a request
        # use, so pin it explicitly rather than inheriting ALLOWED_HOSTS order.
        domain = settings.SITE_DOMAIN
        if not domain:
            domain = (
                settings.ALLOWED_HOSTS[0]
                if settings.ALLOWED_HOSTS and settings.ALLOWED_HOSTS[0] != '*'
                else 'localhost:8000'
            )
            self.stdout.write(
                self.style.WARNING(
                    f'SITE_DOMAIN is not set — falling back to the first '
                    f'ALLOWED_HOSTS entry ({domain}). Set SITE_DOMAIN '
                    f'explicitly so reordering ALLOWED_HOSTS cannot silently '
                    f'change the recorded site domain.'
                )
            )

        site, _ = Site.objects.update_or_create(
            id=settings.SITE_ID,
            defaults={'domain': domain, 'name': 'WeAreChecking'},
        )

        app, created = SocialApp.objects.update_or_create(
            provider='discord',
            defaults={
                'name': 'Discord',
                'client_id': settings.DISCORD_CLIENT_ID,
                'secret': settings.DISCORD_CLIENT_SECRET,
            },
        )
        app.sites.set([site])

        action = 'Created' if created else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(f'{action} Discord OAuth app for site: {domain}')
        )
