from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.crypto import get_random_string

from events.models import ADMIN_KEY_LENGTH, Event


class Command(BaseCommand):
    help = (
        'Rotate every Event.admin_key and delete every session. For use after '
        'a credential exposure: both are credentials stored in the database, '
        'so reading the database is enough to hold them. Rotating admin keys '
        'invalidates every shareable admin link that has ever been handed '
        'out, so prefer --dry-run first.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be rotated without changing anything.',
        )
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument(
            '--keys-only',
            action='store_true',
            help='Rotate admin keys and leave sessions alone.',
        )
        scope.add_argument(
            '--sessions-only',
            action='store_true',
            help='Delete sessions and leave admin keys alone. The cheap half: '
                 'everyone signs in with Discord again and nothing else breaks.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        do_keys = not options['sessions_only']
        do_sessions = not options['keys_only']

        event_count = Event.objects.count() if do_keys else 0
        session_count = Session.objects.count() if do_sessions else 0

        if dry_run:
            if do_keys:
                self.stdout.write(
                    'Would rotate the admin key of %d event(s).' % event_count
                )
            if do_sessions:
                self.stdout.write(
                    'Would delete %d session(s).' % session_count
                )
            self.stdout.write('Dry run — nothing was changed.')
            return

        # One transaction so a failure part-way leaves the operator with a
        # known state rather than half-rotated keys.
        with transaction.atomic():
            if do_keys:
                self._rotate_admin_keys()
            if do_sessions:
                Session.objects.all().delete()

        if do_keys:
            self.stdout.write(self.style.SUCCESS(
                'Rotated the admin key of %d event(s).' % event_count
            ))
            if event_count:
                self.stdout.write(self.style.WARNING(
                    'Every previously shared admin link is now dead. Event '
                    'owners need the new one from /<event_id>/admin/.'
                ))
        if do_sessions:
            self.stdout.write(self.style.SUCCESS(
                'Deleted %d session(s) — everyone is signed out.'
                % session_count
            ))

    def _rotate_admin_keys(self):
        """
        Assign a fresh key to every event in one UPDATE.

        Event.save() only fills admin_key when it is empty, so rotation has to
        assign the new value itself rather than clearing the field and saving.

        The new keys are deliberately not printed. They are credentials, and
        this command's output goes to the same stdout stream that
        config.logging works to keep admin keys out of.
        """
        events = list(Event.objects.only('id', 'admin_key'))
        for event in events:
            event.admin_key = get_random_string(ADMIN_KEY_LENGTH)
        if events:
            Event.objects.bulk_update(events, ['admin_key'], batch_size=500)
