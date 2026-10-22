import csv
import os

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.crypto import get_random_string

from events.models import ADMIN_KEY_LENGTH, Event

MANIFEST_HEADER = ['event_id', 'event_name', 'admin_path', 'discord_owner']


class Command(BaseCommand):
    help = (
        'Rotate every Event.admin_key and delete every session. For use after '
        'a credential exposure: both are credentials stored in the database, '
        'so reading the database is enough to hold them. Rotating admin keys '
        'invalidates every shareable admin link that has ever been handed '
        'out, so start with --dry-run.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be rotated without changing anything.',
        )
        parser.add_argument(
            '--yes',
            action='store_true',
            help='Required for a real run. None of this can be undone.',
        )
        parser.add_argument(
            '--output',
            metavar='PATH',
            help='Where to write the new admin keys. Required when rotating '
                 'keys: they are the only way back into an event whose '
                 'creator was never signed in, and they are never printed. '
                 'Written 0600, and an existing file is not overwritten.',
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
        output = options['output']

        if output and not do_keys:
            raise CommandError(
                '--output has nothing to write without a key rotation.'
            )

        if dry_run:
            self._report_plan(do_keys, do_sessions, output)
            return

        if not options['yes']:
            raise CommandError(
                'This cannot be undone. Re-run with --yes, or with --dry-run '
                'to see what it would do.'
            )

        # Without a manifest the new keys exist only in the database, which no
        # page renders — an event whose created_by is NULL would be left with
        # no way in at all. Refuse rather than produce that state.
        if do_keys and not output:
            raise CommandError(
                'Rotating admin keys needs --output PATH. The new keys are '
                'never printed, and for an event created by someone who was '
                'not signed in they are the only remaining way in.'
            )

        rows = []
        session_count = 0
        # One transaction, and the manifest is written inside it: if the file
        # cannot be written the rotation rolls back rather than stranding keys
        # that nobody holds.
        with transaction.atomic():
            if do_keys:
                rows = self._rotate_admin_keys()
                self._write_manifest(output, rows)
            if do_sessions:
                session_count, _ = Session.objects.all().delete()

        self._report_result(rows, session_count, do_keys, do_sessions, output)

    def _report_plan(self, do_keys, do_sessions, output):
        if do_keys:
            self.stdout.write(
                'Would rotate the admin key of %d event(s).'
                % Event.objects.count()
            )
            orphans = Event.objects.filter(created_by__isnull=True).count()
            if orphans:
                self.stdout.write(
                    '%d of them have no Discord owner, so the new key would be '
                    'the only way in.' % orphans
                )
            if not output:
                self.stdout.write(
                    'A real run needs --output PATH to write the new keys to.'
                )
        if do_sessions:
            self.stdout.write(
                'Would delete %d session(s).' % Session.objects.count()
            )
        self.stdout.write('Dry run — nothing was changed.')

    def _report_result(self, rows, session_count, do_keys, do_sessions, output):
        if do_keys:
            self.stdout.write(self.style.SUCCESS(
                'Rotated the admin key of %d event(s).' % len(rows)
            ))
            if rows:
                self.stdout.write(
                    'New keys are in %s. Every previously shared admin link is '
                    'now dead — hand each owner their new one from that file, '
                    'then delete it.' % output
                )
                orphans = sum(1 for row in rows if not row[3])
                if orphans:
                    self.stdout.write(self.style.WARNING(
                        '%d event(s) have no Discord owner. Nothing in the app '
                        'will ever show their key again, so that file is the '
                        'only way back into them.' % orphans
                    ))
        if do_sessions:
            self.stdout.write(self.style.SUCCESS(
                'Deleted %d session(s) — everyone is signed out.'
                % session_count
            ))

    def _rotate_admin_keys(self):
        """
        Assign a fresh key to every event in one UPDATE, and return the
        manifest rows describing them.

        Event.save() only fills admin_key when it is empty, so rotation has to
        assign the new value itself rather than clearing the field and saving.

        The rows are built from the same list that was written, and the count
        reported to the operator is its length — a count taken before the
        transaction would be wrong for any event created in between.
        """
        events = list(Event.objects.select_related('created_by'))
        for event in events:
            event.admin_key = get_random_string(ADMIN_KEY_LENGTH)
        if events:
            Event.objects.bulk_update(events, ['admin_key'], batch_size=500)

        return [
            [
                str(event.id),
                event.name,
                '/%s/admin/%s/' % (event.id, event.admin_key),
                event.created_by.discord_username if event.created_by else '',
            ]
            for event in events
        ]

    def _write_manifest(self, path, rows):
        """
        Write the new keys where only the operator can read them.

        O_EXCL so a second run cannot quietly overwrite the manifest from the
        first, and 0o600 so the file is never briefly world-readable. The keys
        are deliberately not written to stdout: that is the stream
        config.logging works to keep admin keys out of, and on Railway it is
        captured and retained.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise CommandError(
                '%s already exists — refusing to overwrite a manifest that may '
                'hold the only copy of an earlier rotation. Pick another path.'
                % path
            )
        except OSError as exc:
            raise CommandError('Cannot write %s: %s' % (path, exc))

        with os.fdopen(descriptor, 'w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(MANIFEST_HEADER)
            writer.writerows(rows)
