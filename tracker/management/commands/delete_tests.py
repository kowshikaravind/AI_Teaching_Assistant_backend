from django.core.management.base import BaseCommand
from tracker.models import UpcomingTest, TestMark
from django.db.models import Q


class Command(BaseCommand):
    help = 'Delete tests from the database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--all',
            action='store_true',
            help='Delete ALL upcoming tests',
        )
        parser.add_argument(
            '--scheduled',
            action='store_true',
            help='Delete only scheduled (not active/completed) tests',
        )
        parser.add_argument(
            '--test-id',
            type=int,
            help='Delete a specific test by ID',
        )
        parser.add_argument(
            '--marks-only',
            action='store_true',
            help='Delete only student marks (TestMarks), not the tests themselves',
        )
        parser.add_argument(
            '--status',
            type=str,
            help='Delete tests with specific status (e.g., scheduled, active, finished)',
        )

    def handle(self, *args, **options):
        counts = {'deleted': 0, 'errors': 0}

        try:
            if options['marks_only']:
                # Delete only TestMarks (student marks), not the tests
                deleted, _ = TestMark.objects.all().delete()
                self.stdout.write(
                    self.style.SUCCESS(f'✓ Deleted {deleted} student marks (TestMarks)')
                )
                counts['deleted'] = deleted
            elif options['test_id']:
                # Delete specific test
                try:
                    test = UpcomingTest.objects.get(id=options['test_id'])
                    test_name = test.test_name
                    test.delete()
                    self.stdout.write(
                        self.style.SUCCESS(f'✓ Deleted test: {test_name} (ID: {options["test_id"]})')
                    )
                    counts['deleted'] = 1
                except UpcomingTest.DoesNotExist:
                    self.stdout.write(
                        self.style.ERROR(f'✗ Test with ID {options["test_id"]} not found')
                    )
                    counts['errors'] = 1

            elif options['all']:
                # Delete ALL tests
                count = UpcomingTest.objects.count()
                confirm = input(f'Are you sure you want to delete ALL {count} tests? (yes/no): ')
                if confirm.lower() == 'yes':
                    UpcomingTest.objects.all().delete()
                    self.stdout.write(self.style.SUCCESS(f'✓ Deleted all {count} tests'))
                    counts['deleted'] = count
                else:
                    self.stdout.write('Cancelled.')

            elif options['scheduled']:
                # Delete only scheduled tests (not active or completed)
                scheduled_tests = UpcomingTest.objects.filter(
                    ~Q(status__in=['active', 'finished'])
                )
                count = scheduled_tests.count()
                if count == 0:
                    self.stdout.write(self.style.WARNING('No scheduled tests found'))
                else:
                    confirm = input(f'Delete {count} scheduled tests? (yes/no): ')
                    if confirm.lower() == 'yes':
                        deleted, _ = scheduled_tests.delete()
                        self.stdout.write(
                            self.style.SUCCESS(f'✓ Deleted {deleted} scheduled tests')
                        )
                        counts['deleted'] = deleted
                    else:
                        self.stdout.write('Cancelled.')

            elif options['status']:
                # Delete tests with specific status
                status = options['status'].lower()
                tests = UpcomingTest.objects.filter(status=status)
                count = tests.count()
                if count == 0:
                    self.stdout.write(self.style.WARNING(f'No tests with status "{status}" found'))
                else:
                    confirm = input(f'Delete {count} tests with status "{status}"? (yes/no): ')
                    if confirm.lower() == 'yes':
                        deleted, _ = tests.delete()
                        self.stdout.write(
                            self.style.SUCCESS(f'✓ Deleted {deleted} tests with status "{status}"')
                        )
                        counts['deleted'] = deleted
                    else:
                        self.stdout.write('Cancelled.')

            else:
                # Show usage
                self.stdout.write(
                    self.style.WARNING(
                        'No action specified. Use one of:\n'
                        '  --all: Delete all tests\n'
                        '  --scheduled: Delete scheduled tests\n'
                        '  --test-id <ID>: Delete specific test\n'
                        '  --status <status>: Delete tests with specific status\n'
                        '  --marks-only: Delete only student marks, not tests'
                    )
                )

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'✗ Error: {str(e)}'))
            counts['errors'] = 1

        # Summary
        if counts['deleted'] > 0:
            self.stdout.write(
                self.style.SUCCESS(f'\nSummary: {counts["deleted"]} item(s) deleted successfully')
            )
