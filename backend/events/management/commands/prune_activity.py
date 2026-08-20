from datetime import datetime, timedelta, timezone as dt_utc

from django.conf import settings
from django.core.management.base import BaseCommand

from events.models import ActivityLog


class Command(BaseCommand):
    help = (
        'Delete ActivityLog rows older than ACTIVITY_RETENTION_DAYS. '
        'Intended to run on a schedule; safe to run by hand at any time.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=None,
            help='Override the retention horizon for this run.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be deleted without deleting it.',
        )

    def handle(self, *args, **options):
        days = options['days']
        if days is None:
            days = getattr(settings, 'ACTIVITY_RETENTION_DAYS', 90)

        if days <= 0:
            self.stdout.write(self.style.WARNING(
                'Retention of %d days would delete everything — refusing. '
                'Set ACTIVITY_RETENTION_DAYS to a positive number.' % days
            ))
            return

        cutoff = datetime.now(dt_utc.utc) - timedelta(days=days)
        stale = ActivityLog.objects.filter(occurred_at__lt=cutoff)
        count = stale.count()

        if options['dry_run']:
            self.stdout.write(
                'Would delete %d row(s) older than %s.'
                % (count, cutoff.strftime('%Y-%m-%d %H:%M UTC'))
            )
            return

        stale.delete()
        self.stdout.write(self.style.SUCCESS(
            'Deleted %d activity row(s) older than %s.'
            % (count, cutoff.strftime('%Y-%m-%d %H:%M UTC'))
        ))
